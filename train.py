import functools

import imlib as im
import numpy as np
import pylib as py
import tensorflow as tf
import tensorflow.keras as keras
import tf2lib as tl
import tf2gan as gan
import tqdm
from skimage.measure import compare_ssim

import data
import module


# ==============================================================================
# =                                   param                                    =
# ==============================================================================

py.arg('--dataset', default='horse2zebra')
py.arg('--datasets_dir', default='datasets')
py.arg('--load_size', type=int, default=464)  # load image to this size
py.arg('--crop_size', type=int, default=400)  #then crop to this size, default 256
py.arg('--batch_size', type=int, default=1)
py.arg('--epochs', type=int, default=150) #default 200
py.arg('--epoch_decay', type=int, default=75) #epoch to start decaying learning rate, default 100
py.arg('--lr', type=float, default=0.0002) #default 0.0002
py.arg('--beta_1', type=float, default=0.5)
py.arg('--adversarial_loss_mode', default='lsgan', choices=['gan', 'hinge_v1', 'hinge_v2', 'lsgan', 'wgan'])
py.arg('--gradient_penalty_mode', default='none', choices=['none', 'dragan', 'wgan-gp'])
py.arg('--gradient_penalty_weight', type=float, default=10.0)
py.arg('--cycle_loss_weight', type=float, default=10.0)
py.arg('--identity_loss_weight', type=float, default=0.0)
py.arg('--pool_size', type=int, default=25)  # pool size to store fake samples
args = py.args()

# output_dir
output_dir = py.join('output5-just watching MSe etc ', args.dataset)
py.mkdir(output_dir)

# save settings
py.args_to_yaml(py.join(output_dir, 'settings.yml'), args)


# ==============================================================================
# =                                    data                                    =
# ==============================================================================

A_img_paths = py.glob(py.join(args.datasets_dir, args.dataset, 'trainA'), '*.png')
B_img_paths = py.glob(py.join(args.datasets_dir, args.dataset, 'trainB'), '*.png')
A_B_dataset, len_dataset = data.make_zip_dataset(A_img_paths, B_img_paths, args.batch_size, args.load_size, args.crop_size, training=True, repeat=False)

A2B_pool = data.ItemPool(args.pool_size)
B2A_pool = data.ItemPool(args.pool_size)

A_img_paths_test = py.glob(py.join(args.datasets_dir, args.dataset, 'testA'), '*.png')
B_img_paths_test = py.glob(py.join(args.datasets_dir, args.dataset, 'testB'), '*.png')
A_B_dataset_test, _ = data.make_zip_dataset(A_img_paths_test, B_img_paths_test, args.batch_size, args.load_size, args.crop_size, training=False, repeat=True)


# ==============================================================================
# =                                   models                                   =
# ==============================================================================

G_A2B = module.ResnetGenerator(input_shape=(args.crop_size, args.crop_size, 1))
G_B2A = module.ResnetGenerator(input_shape=(args.crop_size, args.crop_size, 1))

D_A = module.ConvDiscriminator(input_shape=(args.crop_size, args.crop_size, 1))
D_B = module.ConvDiscriminator(input_shape=(args.crop_size, args.crop_size, 1))

d_loss_fn, g_loss_fn = gan.get_adversarial_losses_fn(args.adversarial_loss_mode)
cycle_loss_fn = tf.losses.MeanAbsoluteError()
identity_loss_fn = tf.losses.MeanAbsoluteError()

G_lr_scheduler = module.LinearDecay(args.lr, args.epochs * len_dataset, args.epoch_decay * len_dataset)
D_lr_scheduler = module.LinearDecay(args.lr, args.epochs * len_dataset, args.epoch_decay * len_dataset)
G_optimizer = keras.optimizers.Adam(learning_rate=G_lr_scheduler, beta_1=args.beta_1)
D_optimizer = keras.optimizers.Adam(learning_rate=D_lr_scheduler, beta_1=args.beta_1)


# ==============================================================================
# =                                 train step                                 =
# ==============================================================================

@tf.function
def train_G(A, B):
    with tf.GradientTape() as t:
        A2B = G_A2B(A, training=True)
        B2A = G_B2A(B, training=True)
        A2B2A = G_B2A(A2B, training=True)
        B2A2B = G_A2B(B2A, training=True)
        A2A = G_B2A(A, training=True)
        B2B = G_A2B(B, training=True)

        A2B_d_logits = D_B(A2B, training=True)
        B2A_d_logits = D_A(B2A, training=True)

        A2B_g_loss = g_loss_fn(A2B_d_logits)
        B2A_g_loss = g_loss_fn(B2A_d_logits)
        A2B2A_cycle_loss = cycle_loss_fn(A, A2B2A)
        B2A2B_cycle_loss = cycle_loss_fn(B, B2A2B)
        A2A_id_loss = identity_loss_fn(A, A2A)
        B2B_id_loss = identity_loss_fn(B, B2B)

        G_loss = (A2B_g_loss + B2A_g_loss) + (A2B2A_cycle_loss + B2A2B_cycle_loss) * args.cycle_loss_weight + (A2A_id_loss + B2B_id_loss) * args.identity_loss_weight

    G_grad = t.gradient(G_loss, G_A2B.trainable_variables + G_B2A.trainable_variables)
    G_optimizer.apply_gradients(zip(G_grad, G_A2B.trainable_variables + G_B2A.trainable_variables))

    return A2B, B2A, {'A2B_g_loss': A2B_g_loss,
                      'B2A_g_loss': B2A_g_loss,
                      'A2B2A_cycle_loss': A2B2A_cycle_loss,
                      'B2A2B_cycle_loss': B2A2B_cycle_loss,
                      'A2A_id_loss': A2A_id_loss,
                      'B2B_id_loss': B2B_id_loss}


@tf.function
def train_D(A, B, A2B, B2A):
    with tf.GradientTape() as t:
        A_d_logits = D_A(A, training=True)
        B2A_d_logits = D_A(B2A, training=True)
        B_d_logits = D_B(B, training=True)
        A2B_d_logits = D_B(A2B, training=True)

        A_d_loss, B2A_d_loss = d_loss_fn(A_d_logits, B2A_d_logits)
        B_d_loss, A2B_d_loss = d_loss_fn(B_d_logits, A2B_d_logits)
        D_A_gp = gan.gradient_penalty(functools.partial(D_A, training=True), A, B2A, mode=args.gradient_penalty_mode)
        D_B_gp = gan.gradient_penalty(functools.partial(D_B, training=True), B, A2B, mode=args.gradient_penalty_mode)

        D_loss = (A_d_loss + B2A_d_loss) + (B_d_loss + A2B_d_loss) + (D_A_gp + D_B_gp) * args.gradient_penalty_weight

    D_grad = t.gradient(D_loss, D_A.trainable_variables + D_B.trainable_variables)
    D_optimizer.apply_gradients(zip(D_grad, D_A.trainable_variables + D_B.trainable_variables))

    return {'A_d_loss': A_d_loss + B2A_d_loss,
            'B_d_loss': B_d_loss + A2B_d_loss,
            'D_A_gp': D_A_gp,
            'D_B_gp': D_B_gp}


def train_step(A, B):
    A2B, B2A, G_loss_dict = train_G(A, B)

    # cannot autograph `A2B_pool`
    A2B = A2B_pool(A2B)  # or A2B = A2B_pool(A2B.numpy()), but it is much slower
    B2A = B2A_pool(B2A)  # because of the communication between CPU and GPU

    D_loss_dict = train_D(A, B, A2B, B2A)

    return G_loss_dict, D_loss_dict

#Peforms the translation trafo G on test samples
@tf.function
def sample(A, B):
    A2B = G_A2B(A, training=False)
    B2A = G_B2A(B, training=False)
    A2B2A = G_B2A(A2B, training=False)
    B2A2B = G_A2B(B2A, training=False)
    return A2B, B2A, A2B2A, B2A2B


# ==============================================================================
# =                                    run                                     =
# ==============================================================================

# epoch counter
ep_cnt = tf.Variable(initial_value=0, trainable=False, dtype=tf.int64)

# checkpoint
checkpoint = tl.Checkpoint(dict(G_A2B=G_A2B,
                                G_B2A=G_B2A,
                                D_A=D_A,
                                D_B=D_B,
                                G_optimizer=G_optimizer,
                                D_optimizer=D_optimizer,
                                ep_cnt=ep_cnt),
                           py.join(output_dir, 'checkpoints'),
                           max_to_keep=5)
try:  # restore checkpoint including the epoch counter
    checkpoint.restore().assert_existing_objects_matched()
except Exception as e:
    print(e)

# summary
train_summary_writer = tf.summary.create_file_writer(py.join(output_dir, 'summaries', 'train'))

'''defining translation quality function, such as MSE, NCC, SSIM ...'''
def MSE(target_img,styled_img):
    '''Computing the Mean Squared Error - Using the tf math package
    MSE -> 0 is great'''
    if len(styled_img.shape) > 3:
        styled_img = tf.squeeze(styled_img, axis = 0)
    if len(target_img.shape) > 3:
        target_img = tf.squeeze(target_img, axis = 0)
    error = tf.math.square(tf.math.subtract(target_img, styled_img))
    se = tf.math.reduce_sum(error)
    mse = se/tf.size(error, out_type=tf.dtypes.float32)
    return np.float(mse) #Converting tf.type to np.float for printing'''

    
def NCC(target_img, styled_img):
    '''Computing the Normalized Cross correlation of two images
    which is mathematically the cosine of angle between 
    flattened out image arrays as 1D-vectors
    NCC -> 1 is great'''
    flat_target = tf.reshape(target_img, [-1])
    flat_styled = tf.reshape(styled_img, [-1])
    flat_target = np.array(flat_target)
    flat_styled = np.array(flat_styled)
    dot_product = np.dot(flat_target, flat_styled)
    norm_product = np.linalg.norm(flat_target) * np.linalg.norm(flat_styled)
    ncc = dot_product / norm_product
    return ncc

def SSIM(target_img, styled_img):
    '''Computing the Structural Similarity Index SSIM 
    using the skimage.metrics package'''
    if len(styled_img.shape) > 3:
        styled_img = tf.squeeze(styled_img, axis = 0)
    if len(target_img.shape) > 3:
        target_img = tf.squeeze(target_img, axis = 0)
    target_img = np.array(target_img)
    styled_img = np.array(styled_img)
    ssim = compare_ssim(target_img, styled_img, multichannel=True)
    return ssim   


# getting sample of testset - translation is performed on these
test_iter = iter(A_B_dataset_test)
sample_dir = py.join(output_dir, 'samples_training')
py.mkdir(sample_dir)

# main loop
with train_summary_writer.as_default():
    for ep in tqdm.trange(args.epochs, desc='Epoch Loop'):
        if ep < ep_cnt:
            continue

        # update epoch counter
        ep_cnt.assign_add(1)

        # train for an epoch
        for A, B in tqdm.tqdm(A_B_dataset, desc='Inner Epoch Loop', total=len_dataset):
            G_loss_dict, D_loss_dict = train_step(A, B)

            # # summary
            tl.summary(G_loss_dict, step=G_optimizer.iterations, name='G_losses')
            tl.summary(D_loss_dict, step=G_optimizer.iterations, name='D_losses')
            tl.summary({'learning rate': G_lr_scheduler.current_learning_rate}, step=G_optimizer.iterations, name='learning rate')

            # sample
            if G_optimizer.iterations.numpy() % 100 == 0:
                A, B = next(test_iter)
                A2B, B2A, A2B2A, B2A2B = sample(A, B)
                img_sum = im.immerge(np.concatenate([A, A2B, A2B2A, B, B2A, B2A2B], axis=0), n_rows=2)
                print('MSE before GAN: ', MSE(im.immerge(B), im.immerge(A)))
                print('MSE after GAN: ', MSE(im.immerge(B), im.immerge(A2B)))
                print('NCC before GAN: ', NCC(im.immerge(B), im.immerge(A)))
                print('NCC after GAN: ', NCC(im.immerge(B), im.immerge(A2B)))
                print('SSIM before GAN: ', SSIM(im.immerge(B), im.immerge(A)))
                print('SSIM after GAN: ', SSIM(im.immerge(B), im.immerge(A2B)))
                im.imwrite(img_sum, py.join(sample_dir, 'iter-%09d-overview.png' % G_optimizer.iterations.numpy()))
                im.imwrite(im.immerge(A), py.join(sample_dir, 'iter-%09d-orginal-cbct.png' % G_optimizer.iterations.numpy()))
                im.imwrite(im.immerge(A2B), py.join(sample_dir, 'iter-%09d-cbct2ct.png' % G_optimizer.iterations.numpy()))
                im.imwrite(im.immerge(B), py.join(sample_dir, 'iter-%09d-target-ct.png' % G_optimizer.iterations.numpy()))
        # save checkpoint
        checkpoint.save(ep)

'Following up a Style Transfer with translated A2B/CBCT2CT image'
A2B, B2A,A2B2A, B2A2B = sample(A,B)
cbct2ct = A2B 
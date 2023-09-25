import sys
sys.path.insert(1, 'utils')
import os.path
import argparse
import time
import numpy as np
import logging
import torch
import torch.nn as nn
import optuna
import time
from matplotlib import pyplot as plt
from skimage.io import imread
from PIL import Image

from utils import utils_logger
from utils import utils_image as util
from utils import utils_option as option
from utils import utils_pnp as pnp
from models.drunet.network_unet import UNetRes as net

'''
# ----------------------------------------
# Step--1 (prepare opt and create dict)
# ----------------------------------------
'''

json_path='options/optuna_options.json'

parser = argparse.ArgumentParser()
parser.add_argument('--opt', type=str, default=json_path, help='Path to option JSON file.')
parser.add_argument('--launcher', default='pytorch', help='job launcher')
parser.add_argument('--local_rank', type=int, default=0)
parser.add_argument('--dist', default=False)

opt = option.parse(parser.parse_args().opt, is_train=True)

# ----------------------------------------
# return None for missing key
# ----------------------------------------
opt = option.dict_to_nonedict(opt)

# Create directories
out_dir = opt['path']['log']
xk_images_dir = os.path.join(out_dir,"xk")
zk_images_dir = os.path.join(out_dir,"zk")
opt_hist_dir = os.path.join(out_dir,"opt_history")
for dir_path in [out_dir, xk_images_dir, zk_images_dir, opt_hist_dir]:
    if not os.path.isdir(dir_path):
        os.mkdir(dir_path)

# Set logger
logger_name = 'optuna_hparams'
utils_logger.logger_info(logger_name, os.path.join(opt['path']['log'], logger_name + '.log'))
logger = logging.getLogger(logger_name)
logger.info(option.dict2str(opt))

'''
# ------------------------------------------------------------
# Step--2 (create dataloader) TODO: cargar imagen a ajustar
# ------------------------------------------------------------
'''

message = 'Loading images'
logger.info(message)

try:
    H_paths = util.get_image_paths(opt["datasets"]["train"]["dataroot_H"])
except Exception as e:
     logger.info(f"Error loading images path. Exiting with following exception:\n{str(e)}")
     exit()

message = f'Dataset loaded.'
logger.info(message)

"""  
# ----------------------------
Step--3 load denoiser prior
# ----------------------------
"""
message = 'Loading denoiser model'
logger.info(message)
try:
    # load model
    denoiser_model_path = os.path.join(opt["path"]["pretrained_netG"])
    denoiser_model = net(in_nc=1+1, out_nc=1, nc=[64, 128, 256, 512], nb=4, act_mode='R', downsample_mode="strideconv", upsample_mode="convtranspose", bias=False)
    denoiser_model.load_state_dict(torch.load(denoiser_model_path), strict=True)
    denoiser_model.eval()
    for _, v in denoiser_model.named_parameters():
        v.requires_grad = False
except Exception as e:
     logger.info(f"Error loading denoiser model. Exiting with following exception:\n{str(e)}")
     exit()


def define_metric(metric_str):

    metric_dict = {}

    if metric_str == 'PSNR':
        metric_dict['func'] = util.calculate_psnr
        metric_dict['direction'] = 'maximize'
        metric_dict['name'] = 'PSNR'

    elif metric_str == 'SSIM':
        metric_dict['func'] = util.calculate_ssim
        metric_dict['direction'] = 'maximize'
        metric_dict['name'] = 'SSIM'
    
    elif metric_str == 'CER':
        metric_dict['func'] = util.calculate_cer_wer
        metric_dict['direction'] = 'minimize'
        metric_dict['name'] = 'CER'

    elif metric_str == 'edgeJaccard':
        metric_dict['func'] = util.calculate_edge_jaccard
        metric_dict['direction'] = 'maximize'
        metric_dict['name'] = 'edgeJaccard'

    else:
        # If none of above, choose MSE
        metric_dict['func'] = nn.MSELoss()
        metric_dict['direction'] = 'minimize'
        metric_dict['name'] = 'MSE'
    
    return metric_dict

def train_model(trial, dataset, metric_dict, denoiser_model=denoiser_model, pnp_opt=opt['plugnplay']):

    metric = metric_dict['func']
    metric_direction = metric_dict['direction']

    best_metric = -1e6*(metric_direction=='maximize') + 1e6*(metric_direction=='minimize')

    idx = 0

    # Fixing image shape as 800x1000 for faster computation
    total_height, total_width = 800, 1000

    # Plug and Play options
    noise_level_model = pnp_opt["noise_sigma"]/255.0
    modelSigma1 = pnp_opt["sigma1"]
    modelSigma2 = pnp_opt["sigma2"]
    num_iter = pnp_opt["iters_pnp"]
    max_iter_data_term = pnp_opt["iters_data_term"]
    lr = pnp_opt["lr_data_term"]
    lam = pnp_opt["lambda"]
    eps_data_term = 1e-4
    k_print_data_term = 50
    sigma_blur = 5

    degradation = 'hdmi'

    # Time tracker
    since = time.time()

    for i, H_path in enumerate(dataset):

        logger.info(f"Running Plug and Play {i+1}/{len(dataset)}\nImage {H_path}")
        
        idx += 1

        # Load original image red channel
        x_gt_np = imread(H_path)[:,:,0]

        # Crear 4 cuadrados
        # x_gt_np = np.zeros((400,500),dtype='uint8')
        # x_gt_np[150:250,200:300] = 255
        # x_gt_np = np.hstack([x_gt_np,x_gt_np])
        # x_gt_np = np.vstack([x_gt_np,x_gt_np])

        height, width = x_gt_np.shape
        center_h, center_w = height//2, width//2

        # Crop image to size (total_height, total_width)
        x_gt_np = x_gt_np[center_h-total_height//2 : center_h+total_height//2, center_w-total_width//2 : center_w+total_width//2]

        # To tensor float image
        x_gt = util.uint2single(x_gt_np)
        x_gt = torch.tensor(x_gt)

        # TODO: Run PNP with pnp_opt
        
        total_pixels = x_gt.shape[0] * x_gt.shape[1]

        # store |z_k+1 - z^k| 
        # diff_z_record = []
        
        # # store |z_k - x_gt| 
        # diff_x_gt_record = []

        y_obs = pnp.observation(degradation, x_gt, noise_level_model, sigma_blur)
        # y_obs_save = y_obs.detach()
        logger.info("Save observation y")
        # Absolute value
        y_abs_np = util.tensor2single(torch.abs(y_obs))
        y_abs_np = (255*(y_abs_np-y_abs_np.min())/(y_abs_np.max()-y_abs_np.min())).astype('uint8')
        y_abs_outpath = os.path.join(out_dir,"y_abs.png")
        Image.fromarray(y_abs_np).save(y_abs_outpath)
        # Real value
        y_real_np = util.tensor2single(torch.real(y_obs))
        y_real_np = (255*(y_real_np-y_real_np.min())/(y_real_np.max()-y_real_np.min())).astype('uint8')
        y_real_outpath = os.path.join(out_dir,"y_real.png")
        Image.fromarray(y_real_np).save(y_real_outpath)
        # Imag value
        y_imag_np = util.tensor2single(torch.imag(y_obs))
        y_imag_np = (255*(y_imag_np-y_imag_np.min())/(y_imag_np.max()-y_imag_np.min())).astype('uint8')
        y_imag_outpath = os.path.join(out_dir,"y_imag.png")
        Image.fromarray(y_imag_np).save(y_imag_outpath)

        # precalculation of parameters for each iteration
        alphas, sigmas = pnp.get_alpha_sigma(sigma=max(0.255/255., noise_level_model), 
                                             iter_num = num_iter, modelSigma1 = modelSigma1, modelSigma2 = modelSigma2, 
                                             w = 1.0, lam = lam)
        
        logger.info(f"Alphas\n{alphas}")

        logger.info(f"Sigmas\n{sigmas}")

        alphas, sigmas = torch.tensor(alphas), torch.tensor(sigmas)

        # Get initializations z0 and x0 from observation y
        
        # Real part of observation
        z_0 = torch.tensor(y_real_np/255.)
        x_0 = z_0.clone()

        z_opt = z_0
        x_0_data_term = x_0 

        logger.info("Save initialization")
        z0_outpath = os.path.join(zk_images_dir,"z_0.png")
        Image.fromarray(util.tensor2uint(z_0.clone().detach())).save(z0_outpath)
        x0_outpath = os.path.join(xk_images_dir,"x_0.png")
        Image.fromarray(util.tensor2uint(x_0.clone().detach())).save(x0_outpath)

        # iterate algorithm num_iter times
        for pnp_iter in range(num_iter):
            
            logger.info('Plug & Play iteration {}'.format(pnp_iter+1))
            
            # z_prev = z_opt.detach().clone()

            # optimize data term
            logger.info(f"Executing data-term optimization at iter {pnp_iter+1}")
            x_i, optim_history_i, energy_history_i, alpha_history_i = pnp.optimize_data_term(degradation, x_gt, z_opt, x_0_data_term, y_obs, pnp_iter, 
                                                                    sigma_blur, total_pixels, alpha = alphas[pnp_iter], 
                                                                    max_iter = max_iter_data_term, eps = eps_data_term, 
                                                                    lr = lr, k_print = k_print_data_term, plot = False)
            
            logger.info("Save output of data term optimization")
            xk_save = x_i.clone().detach()
            # normalize z_opt between [0, 1]. [H,W]
            min_xk_save = xk_save.min()
            max_xk_save = xk_save.max()
            xk_save = (xk_save - min_xk_save)/(max_xk_save - min_xk_save)
            xk_outpath = os.path.join(xk_images_dir,f"trial{trial.number}_x_{pnp_iter+1}.png")
            Image.fromarray(util.tensor2uint(xk_save)).save(xk_outpath)

            # Save optimization history of dataterm
            optim_history_outpath = os.path.join(opt_hist_dir,f"trial{trial.number}_dataterm_hist_iter{pnp_iter+1}")
            _, ax = plt.subplots(2,2,figsize = (12,8))

            iters_array = np.arange(len(optim_history_i)) + 1
            optim_hist_i_norm = np.array(optim_history_i) / total_pixels
            energy_hist_i_norm = np.array(energy_history_i) / total_pixels
            alpha_hist_i_norm = np.array(alpha_history_i) / total_pixels

            ax[0,0].plot(iters_array, optim_hist_i_norm, '*-r', label='Objective function')
            ax[0,0].plot(iters_array, energy_hist_i_norm, '*--g', label='Energy term')
            ax[0,0].plot(iters_array, alpha_hist_i_norm, '*--b', label='Alpha term')
            ax[0,0].set_xlabel("Data term iterations")
            ax[0,0].grid()
            ax[0,0].legend()

            ax[0,1].plot(iters_array, optim_hist_i_norm, '*--r', label='Objective Function')
            ax[0,1].set_xlabel("Data term iterations")
            ax[0,1].grid()
            ax[0,1].legend()

            ax[1,0].plot(iters_array, energy_hist_i_norm, '*--g', label='Energy term')
            ax[1,0].set_xlabel("Data term iterations")
            ax[1,0].grid()
            ax[1,0].legend()

            ax[1,1].plot(iters_array, alpha_hist_i_norm, '--b', label='Alpha term')
            ax[1,1].set_xlabel("Data term iterations")
            ax[1,1].grid()
            ax[1,1].legend()

            plt.tight_layout()
            # Save as pdf
            # plt.savefig(f"{optim_history_outpath}.pdf", format="pdf",bbox_inches='tight') 
            # Save as png
            plt.savefig(f"{optim_history_outpath}.png", format="png",bbox_inches='tight') 

            # initial condition of data term optimization in k'th iteration of plug&play algorithm is the solution of data term optiization in k-1'th iteration of plug&play
            x_0_data_term = xk_save.clone()

            # adjust dimensions
            x_i = xk_save.detach().numpy()
            # [H,W] --> [H, W, 1]
            x_i = np.expand_dims(x_i, axis=2)
            x_i_dim4 = util.single2tensor4(x_i)
            x_i_dim4 = torch.cat((x_i_dim4, torch.FloatTensor([sigmas[pnp_iter]]).repeat(1, 1, x_i_dim4.shape[2], x_i_dim4.shape[3])), dim=1)

            # forward denoiser model
            logger.info('Enter Denoiser. Sigma = {}. Iteration {}'.format(sigmas[pnp_iter], pnp_iter+1))
            z_opt = denoiser_model(x_i_dim4)
            z_opt = z_opt[0,0,:,:]

            # normalize z_opt between [0, 1]. [H,W]
            min_z_opt = z_opt.min()
            max_z_opt = z_opt.max()
            z_opt = (z_opt - min_z_opt)/(max_z_opt - min_z_opt)

            logger.info("Save output of denoiser model")
            zk_outpath = os.path.join(zk_images_dir,f"trial{trial.number}_z_{pnp_iter+1}.png")
            Image.fromarray(util.tensor2uint(z_opt.clone().detach())).save(zk_outpath)

            # z_next = z_opt.detach().clone()
            
            # # calculate |z_k+1 - z_k| / total_pixels
            # diff_z = torch.norm(z_next - z_prev).detach()
            # diff_z_record.append(diff_z)

            # # calculate |z_k - x_gt| / total_pixels
            # diff_x_gt = torch.norm(z_next - x_gt).detach()
            # diff_x_gt_record.append(diff_x_gt)
        
            # Compute metric between original and restored images 
            cer_metric, wer_metric = metric(util.tensor2uint(z_opt.clone().detach()), util.tensor2uint(x_gt))
            current_metric = cer_metric

            # Update if validation metric is better (lower when minimizing, greater when maximizing)
            maximizing = ( (current_metric > best_metric) and metric_dict['direction'] == 'maximize')
            minimizing = ( (current_metric < best_metric) and metric_dict['direction'] == 'minimize') 

            current_metric_is_better = maximizing or minimizing                       

            if current_metric_is_better:
                best_metric = current_metric
            
            logger.info(f"Iter {pnp_iter+1} metric: {current_metric}")
            trial.report(current_metric, pnp_iter+1) 


    # Whole optuna parameters searching time
    time_elapsed = time.time() - since
    logger.info('Trial {}: training completed in {:.0f}hs {:.0f}min {:.0f}s'.format(
        trial.number ,time_elapsed // (60*60), (time_elapsed // 60)%60, time_elapsed % 60))

    return best_metric

# Define optuna objective function
def objective(trial):

    # Set suggestions for trial

    # trial_lambda = trial.suggest_float("lambda", 1e-3, 3)
    # opt['plugnplay']['lambda'] = trial_lambda

    # trial_iters_pnp = trial.suggest_int("iters_pnp", 2, 3) #TODO: between 3 and 10
    # opt['plugnplay']['iters_pnp'] = trial_iters_pnp

    # trial_sigma1 = trial.suggest_float("sigma1", 10, 50)
    # opt['plugnplay']['sigma1'] = trial_sigma1

    # sigma2 < sigma1. Force it to be 9 stdev less tops
    # trial_sigma2 = trial.suggest_float("sigma2", 1, 10)
    # opt['plugnplay']['sigma2'] = trial_sigma2

    message = f'Trial number {trial.number} with parameters:\n'
    # message = message+f'lambda = {trial_lambda}\n'
    # message = message+f'iters_pnp = {trial_iters_pnp}\n'
    # message = message+f'sigma1 = {trial_sigma1}\n'
    # message = message+f'sigma2 = {trial_sigma2}'

    logger.info(message)

    # Select metric specified at options
    metric_dict = define_metric(opt['optuna']['metric'])

    best_metric = train_model(trial, H_paths, metric_dict, denoiser_model=denoiser_model, pnp_opt=opt['plugnplay'])    

    # Return metric (Objective Value) of the current trial

    return best_metric

def save_optuna_info(study):

    root_dir = out_dir

    # Save page for plot contour for the two most important params
    params_importance = optuna.importance.get_param_importances(study)
    two_importanter_params = sorted(params_importance, key=params_importance.get, reverse=True)[:2]
    fig = optuna.visualization.plot_contour(study, params=two_importanter_params)
    fig.write_html(os.path.join(root_dir,'optuna_plot_contour.html'))
    # Save page for plot slice
    fig = optuna.visualization.plot_slice(study)
    fig.write_html(os.path.join(root_dir,'optuna_plot_slice.html'))
    # Save page for hyperparameters importances
    fig = optuna.visualization.plot_param_importances(study)
    fig.write_html(os.path.join(root_dir,'optuna_plot_param_importances.html'))
    # Save page for optimization history
    fig = optuna.visualization.plot_optimization_history(study)
    fig.write_html(os.path.join(root_dir,'optuna_plot_optimization_history.html'))
    # Save page for intermediate values plot
    fig = optuna.visualization.plot_intermediate_values(study)
    fig.write_html(os.path.join(root_dir,'optuna_plot_intermediate_values.html'))
    # Save page for parallel coordinate plot
    fig = optuna.visualization.plot_parallel_coordinate(study)
    fig.write_html(os.path.join(root_dir,'optuna_plot_parallel_coordinate.html'))

    return


'''
# ----------------------------------------
# Step--3 (setup optuna hyperparameter search)
# ----------------------------------------
'''
metric_dict = define_metric(opt['optuna']['metric'])
sampler = optuna.samplers.TPESampler()
study = optuna.create_study(
        sampler=sampler,
        pruner=optuna.pruners.MedianPruner( 
            n_startup_trials=10, n_warmup_steps=4, interval_steps=2
        ),
        direction=metric_dict['direction'])

study.optimize(func=objective, n_trials=opt['optuna']['n_trials'])

message = 'Best trial:\n'+str(study.best_trial)
logger.info(message)

logger.info('Saving study information at ' + out_dir)
save_optuna_info(study)

logger.info('Hyperparameters study ended')
#/home/ndelafuente/TSR/train/Split-CIFAR100/EWC_baseline.py
# FILE TO TRAIN EWC BASELINE on Split-CIFAR100

import torch
from torch import nn, utils
import torch.nn.functional as F
import wandb 
from copy import deepcopy

from torch.utils.data import DataLoader
from torchvision import models, datasets, transforms

import numpy as np
import matplotlib as mpl
import matplotlib.pyplot as plt
import pandas as pd
from PIL import Image
from tqdm import tqdm
import os
import sys
import random
import time

# Add the project root directory to PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from networks.backbones import ResNet50, MobileNetV2, EfficientNetB0 
from networks.networks_baseline import MultitaskModel_Baseline, TaskHead_Baseline

from utils import *

# ------------------ EWC Auxiliary Functions ------------------ #
def compute_fisher(model, dataset_loader, device, sample_size=200):
    """
    Compute Fisher Information for EWC.
    We sample a subset of the dataset (sample_size) to approximate.
    """
    model.eval()
    fisher = {n: torch.zeros(p.shape, device=device) for n, p in model.named_parameters() if p.requires_grad}
    count = 0
    for i, (x, y, task_ids) in enumerate(dataset_loader):
        x, y = x.to(device), y.to(device)
        model.zero_grad()
        # Get loss
        pred = model(x, task_ids[0])
        loss = F.nll_loss(F.log_softmax(pred, dim=1), y)
        loss.backward()
        for n, p in model.named_parameters():
            if p.requires_grad and p.grad is not None:
                fisher[n] += p.grad.data.pow(2)
        count += 1
        if count * dataset_loader.batch_size >= sample_size:
            break

    # Average fisher
    for n in fisher:
        fisher[n] = fisher[n] / count
    return fisher

def ewc_loss(model, old_params, fisher, ewc_lambda):
    """
    Compute EWC penalty term.
    """
    loss = 0.0
    for n, p in model.named_parameters():
        if p.requires_grad and n in old_params:
            loss += (fisher[n] * (p - old_params[n]).pow(2)).sum()
    return ewc_lambda * loss

# ------------------ Main Training Script ------------------ #

config = config_load(sys.argv[1])["config"]

device = torch.device(config["misc"]["device"] if torch.cuda.is_available() else "cpu")
seed_everything(config['misc']['seed'])

num = time.strftime("%Y%m%d-%H%M%S")
name_run = f"{config['logging']['name']}-{num}"
results_dir = os.path.join(config["logging"]["results_dir"], name_run)
os.makedirs(results_dir, exist_ok=True)

logger = logger(results_dir)

#Log initial info
logger.log(f"Starting training for {config['logging']['name']}")
logger.log(f"Configuration: {config}")
logger.log(f"Device: {device}")
logger.log(f"Random seed: {config['misc']['seed']}")

#Load dataset
data = setup_dataset(dataset_name = config["dataset"]["dataset"],
                     data_dir = config["dataset"]["data_dir"], 
                     num_tasks=config["dataset"]["NUM_TASKS"],
                     val_frac=config["dataset"]["VAL_FRAC"],
                     test_frac=config["dataset"]["TEST_FRAC"],
                     batch_size=config["dataset"]["BATCH_SIZE"])

backbone_dict = {
    'resnet50': ResNet50,
    'mobilenetv2': MobileNetV2,
    'efficientnetb0': EfficientNetB0
}
backbone_name = config["model"]["backbone"]

backbone = backbone_dict[backbone_name](device=device, pretrained=True)
logger.log(f"Using backbone: {backbone_name}")

if config["model"]["frozen_backbone"] == True:
    for param in backbone.parameters():
        param.requires_grad = False

#Create model
baseline_ewc = MultitaskModel_Baseline(backbone, device)

logger.log(f"Model created!")
logger.log(f"Model initialized with freeze_backbone={config['model']['frozen_backbone']}, config={config['model']}")

#Initialize optimizer and loss
optimizer = setup_optimizer(
                model=baseline_ewc,
                lr=config["training"]["lr"],
                l2_reg=config["training"]["l2_reg"],
                optimizer=config["training"]["optimizer"]
            )
loss_fn = nn.CrossEntropyLoss()

#Track metrics for plotting
metrics = { 'train_losses': [],
            'train_accs': [],
            'val_losses': [],
            'val_accs': [],
            'epoch_steps': [], 
            'CL_timesteps': [],
            'best_val_acc': 0.0,
            'steps_trained': 0,
            'soft_losses': [], # Not used now, but kept to maintain code structure
           }

prev_test_accs = []

logger.log(f"Starting training for {config['logging']['name']}")

# EWC parameters storage
old_params = None
fisher = None
ewc_lambda = config["training"]["ewc_lambda"]

with wandb.init(project='HyperCMTL', name=f'{name_run}', config=config) as run:
    #count_optimizer_parameters(optimizer, logger)
    
    #Outer loop for each task, in sequence
    for t, (task_train, task_val) in data['timestep_tasks'].items():
        task_train.num_classes = len(data['timestep_task_classes'][t])
        logger.log(f"Task {t}: {task_train.num_classes} classes\n: {data['task_metadata'][t]}")
        
        if t not in baseline_ewc.task_heads:
            task_head = TaskHead_Baseline(input_size=baseline_ewc.backbone.num_features, 
                                          projection_size=config["model"]["task_head_projection_size"],
                                          num_classes=task_train.num_classes,
                                          device=device)
            #Add task head to model
            baseline_ewc.add_task(t, task_head)
            optimizer.add_param_group({'params': task_head.parameters()})
            logger.log(f"Task head added for task {t}")
            
        #Build training and validation dataloaders
        train_loader, val_loader = [utils.data.DataLoader(d,
                                        batch_size=config["dataset"]["BATCH_SIZE"],
                                        shuffle=True) for d in (task_train, task_val)]
        
        #Inner loop for training epochs over the current task
        for e in range(config['training']['epochs_per_timestep']):
            epoch_train_losses, epoch_train_accs = [], []
            epoch_soft_losses = [] # Not used for EWC, but retained for consistency
            
            progress_bar = tqdm(train_loader, ncols=100, total=len(train_loader), desc=f"Task {t}, Epoch {e}") if config["logging"]["show_progress"] else train_loader
            
            #Training loop
            for batch_idx, batch in enumerate(progress_bar):
                x, y, task_ids = batch
                x, y = x.to(device), y.to(device)
                task_id = task_ids[0]
                
                optimizer.zero_grad()
                
                pred = baseline_ewc(x, task_id)
                hard_loss = loss_fn(pred, y)
                
                # EWC penalty if not the first task
                penalty = 0.0
                if t > 0 and old_params is not None and fisher is not None:
                    penalty = ewc_loss(baseline_ewc, old_params, fisher, ewc_lambda)
                
                total_loss = hard_loss + penalty
                
                wandb.log({'hard_loss': hard_loss.item(), 'ewc_penalty': float(penalty), 'train_loss': total_loss.item(), 'epoch': e, 'task_id': t, 'batch_idx': batch_idx})
                
                total_loss.backward()
                optimizer.step()
                
                #Track metrics
                epoch_train_losses.append(total_loss.item())
                epoch_train_accs.append(get_batch_acc(pred, y))
                epoch_soft_losses.append(0.0)  # no soft loss in EWC, just keep to maintain structure
                metrics['steps_trained'] += 1
                
                if config["logging"]["show_progress"]:
                    progress_bar.set_description(f"Task {t}, Epoch {e}, Loss: {total_loss.item():.4f}, Acc: {epoch_train_accs[-1]:.4f}")
                    
            #Evaluate model on current task's validation set after each epoch
            avg_val_loss, avg_val_acc, time_elapsed = evaluate_model_timed(multitask_model=baseline_ewc,
                                                                          val_loader=val_loader,  
                                                                          loss_fn=loss_fn,
                                                                          device=device)
          
            wandb.log({'val_loss': avg_val_loss, 'val_acc': avg_val_acc, 'epoch': e, 'task_id': t, 'time': time_elapsed})
            
            #Update metrics
            metrics['epoch_steps'].append(metrics['steps_trained'])
            metrics['train_losses'].extend(epoch_train_losses)
            metrics['train_accs'].extend(epoch_train_accs)
            metrics['soft_losses'].extend(epoch_soft_losses)
            metrics['val_losses'].append(avg_val_loss)
            metrics['val_accs'].append(avg_val_acc)
            
            if config["logging"]["show_progress"]:
                logger.log((f'E{e} loss:{np.mean(epoch_train_losses):.2f}|v:{avg_val_loss:.2f}' +
                            f'| acc t:{np.mean(epoch_train_accs):>5.1%}|v:{avg_val_acc:>5.1%} in {time_elapsed:.2f}s'))
                
            if avg_val_acc > metrics['best_val_acc']:
                metrics['best_val_acc'] = avg_val_acc
                logger.log(f"New best validation accuracy: {avg_val_acc:.4f}")
                
        #For plotting
        metrics['CL_timesteps'].append(metrics['steps_trained'])
        
        #If plotting is enabled, plot training curves
        if config["logging"]["plot_training"] and len(metrics['val_losses']) > 0:
            training_plot(metrics, show_timesteps=True)
            
        if config["logging"]["verbose"]:
            logger.log(f"Best validation accuracy: {metrics['best_val_acc']:.4f}")
            logger.log(f"Final epoch completed in {time_elapsed:.2f}s")   
        metrics['best_val_acc'] = 0.0
            
        #Evaluate the model on all previous tasks
        metrics_test = test_evaluate_metrics(
                            multitask_model=baseline_ewc,
                            selected_test_sets=data['task_test_sets'][:t+1],
                            task_test_sets=data['task_test_sets'],
                            model_name=f'EWC at t={t}',
                            prev_accs=prev_test_accs,
                            verbose=True,
                            task_metadata=data['task_metadata'],
                            device=device
                            )
            
        wandb.log({**metrics_test, 'task_id': t})
        prev_test_accs.append(metrics_test['task_test_accs'])
        
        # After finishing training task t, compute Fisher and store old params
        baseline_ewc.eval()
        old_params = {n: p.clone().detach() for n, p in baseline_ewc.named_parameters() if p.requires_grad}
        fisher = compute_fisher(baseline_ewc, train_loader, device, sample_size=200)
            
    #Log final metrics
    logger.log(f"Task {t} completed!")
    logger.log(f'final metrics: {metrics_test}')
    wandb.summary.update(metrics_test)

import sys, time, os
import numpy as np
import random
import torch
from copy import deepcopy
import utils
from utils import *
import torch.nn.functional as F
import torch.nn as nn
from torchvision import models
from torchvision.models.resnet import *
import math

sys.path.append('..')
from arguments import get_args

args = get_args()

from bayes_layer import BayesianConv2DVCL, _calculate_fan_in_and_fan_out, BayesianLinearVCL_bias, BayesianLinearVCL

class Appr(object):
    def __init__(self, model, nepochs=100, sbatch=256, lr=0.001, 
                 lr_min=2e-6, lr_factor=3, lr_patience=5, clipgrad=100, args=None, log_name=None, split=False):

        self.model = model
        self.model_old = deepcopy(self.model)
        
        file_name = log_name
        self.logger = utils.logger(file_name=file_name, resume=False, path='./result_data/csvdata/', data_format='csv')

        self.nepochs = nepochs
        self.sbatch = sbatch
        self.lr = lr
        self.lr_rho = args.lr_rho
        self.lr_min = lr / (lr_factor ** 5)
        self.lr_factor = lr_factor
        self.lr_patience = 5
        self.clipgrad = clipgrad
        self.args = args
        self.iteration = 0
        self.epoch = 0
        self.saved = 0
        self.split = split
        self.beta = args.beta
        
        self.drop = [20,40,60,75,90]
        
        self.param_name = []
        
        for (name, p) in self.model.named_parameters():
            self.param_name.append(name)
        
        self.optimizer = self._get_optimizer()
        
        # if len(args.parameter) >= 1:
        #     params = args.parameter.split(',')
        #     print('Setting parameters to', params)
        #     self.lamb = float(params[0])

        return

    def _get_optimizer(self, lr=None, lr_rho = None):
        if lr is None: lr = self.lr
        if lr_rho is None: lr_rho = self.lr_rho
        if args.optimizer == 'Adam':
            return Adam(self.model.parameters(), lr=lr, lr_rho=lr_rho, param_name = self.param_name)
        if args.optimizer == 'SGD':
            return torch.optim.SGD(self.model.parameters(),lr=lr)
    
    def train(self, t, xtrain, ytrain, xvalid, yvalid, data, input_size, taskcla):
        best_loss = np.inf
        best_model = utils.get_model(self.model)
        lr = self.lr
        lr_rho = self.lr_rho
        patience = self.lr_patience
        self.optimizer = self._get_optimizer(lr, lr_rho)

        # Loop epochs
        for e in range(self.nepochs):
            self.epoch = self.epoch + 1
            # Train
            clock0 = time.time()

            num_batch = xtrain.size(0)
            
            self.train_epoch(t, xtrain, ytrain)
            
            clock1 = time.time()
            train_loss, train_acc = self.eval(t, xtrain, ytrain)
            
            clock2 = time.time()
            if (e+1) % 20 == 0:
                print('| Epoch {:3d}, time={:5.1f}ms/{:5.1f}ms | Train: loss={:.3f}, acc={:5.1f}% |'.format(
                    e + 1, 1000 * self.sbatch * (clock1 - clock0) / num_batch,
                    1000 * self.sbatch * (clock2 - clock1) / num_batch, train_loss, 100 * train_acc), end='')
            # Valid
            
            valid_loss, valid_acc = self.eval(t, xvalid, yvalid)
            if (e+1) % 20 == 0:
                print(' Valid: loss={:.3f}, acc={:5.1f}% |'.format(valid_loss, 100 * valid_acc), end='')
                print()
            # save log for current task & old tasks at every epoch
            self.logger.add(epoch=(t * self.nepochs) + e, task_num=t + 1, valid_loss=valid_loss, valid_acc=valid_acc)
            for task in range(t):
                xvalid_t=data[task]['valid']['x'].cuda()
                yvalid_t=data[task]['valid']['y'].cuda()
                    
                valid_loss_t, valid_acc_t = self.eval(task, xvalid_t, yvalid_t)
                self.logger.add(epoch=(t * self.nepochs) + e, task_num=task + 1, valid_loss=valid_loss_t,
                                valid_acc=valid_acc_t)

            if valid_loss < best_loss:
                best_loss = valid_loss
                best_model = utils.get_model(self.model)
                patience = self.lr_patience
                # print(' *', end='')
            else:
                patience -= 1
                if patience <= 0:
                    lr /= self.lr_factor
                    lr_rho /= self.lr_factor
                    # print(' lr={:.1e}'.format(lr), end='')
                    # if lr < self.lr_min:
                    #     print()
                    patience = self.lr_patience
                    self.optimizer = self._get_optimizer(lr, lr_rho)
            # print()

            utils.freeze_model(self.model_old)  # Freeze the weights
            
            
        # Restore best
        utils.set_model_(self.model, best_model)
        self.model_old = deepcopy(self.model)
        self.saved = 1

        self.logger.save()

        return

    def train_epoch(self,t,x,y):
        self.model.train()

        r = np.arange(x.size(0))
        np.random.shuffle(r)
        r = torch.LongTensor(r).cuda()
        scale = args.KL_weight_theta / len(r)
        if t == 0:
            scale = 0
        # Loop batches
        self.cur_task = t

        for i in range(0, len(r), self.sbatch):
            self.iteration += 1
            if i + self.sbatch <= len(r):
                b = r[i:i + self.sbatch]
            else:
                b = r[i:]
            images = x[b]
            targets = y[b]

            # Forward current model
            mini_batch_size = len(targets)
            avg_loss = 0
            for _ in range(args.num_samples):
                if self.split:
                    output = F.log_softmax(self.model(images, sample=True)[t],dim=1)
                else:
                    output = self.model(images, sample=True)
                loss = F.nll_loss(output, targets, reduction='mean')
                loss = self.custom_regularization(self.model_old, self.model, scale, loss)
                avg_loss += loss / args.num_samples
            # Backward
            self.optimizer.zero_grad()
            avg_loss.backward()
            if args.optimizer == 'SGD' or args.optimizer == 'SGD_momentum_decay':
                torch.nn.utils.clip_grad_norm(self.model.parameters(),self.clipgrad)
            self.optimizer.step()

        return

    def eval(self,t,x,y):
        total_loss = 0
        total_acc = 0
        total_num = 0
        self.model.eval()

        r = np.arange(x.size(0))
        r = torch.LongTensor(r).cuda()

        # Loop batches
        with torch.no_grad():
            for i in range(0, len(r), self.sbatch):
                if i + self.sbatch <= len(r):
                    b = r[i:i + self.sbatch]
                else:
                    b = r[i:]
                images = x[b]
                targets = y[b]
                
                # Forward
                mini_batch_size = len(targets)
                if self.split:
                    output = F.log_softmax(self.model(images, sample= False)[t],dim=1)
                else:
                    output = self.model(images, sample=False)
                loss = F.nll_loss(output, targets, reduction= 'sum')

                _, pred = output.max(1)
                hits = (pred == targets).float()
                
                total_loss += loss.data.cpu().numpy()
                total_acc += hits.sum().data.cpu().numpy()
                total_num += len(b)



        return total_loss / total_num, total_acc / total_num

    def criterion(self, t, output, targets):
        # Regularization for all previous tasks
        loss_reg = 0
        if t > 0:
            for (name, param), (_, param_old) in zip(self.model.named_parameters(), self.model_old.named_parameters()):
                loss_reg += torch.sum(self.fisher[name] * (param_old - param).pow(2)) / 2

        return self.ce(output, targets) + self.lamb * loss_reg

    def custom_regularization(self, saver_net, trainer_net, scale, loss):
        
        mu_term = 0
        log_sig_term = 0
        sig_term = 0
        
        
        for (_, saver_layer), (_, trainer_layer) in zip(saver_net.named_children(), trainer_net.named_children()):
            if isinstance(trainer_layer, BayesianLinearVCL_bias)==False and isinstance(trainer_layer, BayesianConv2DVCL)==False and isinstance(trainer_layer, BayesianLinearVCL)==False:
                continue
            # calculate mu regularization
            trainer_weight_mu = trainer_layer.weight_mu
            trainer_weight_sigma = torch.log1p(torch.exp(trainer_layer.weight_rho))

            saver_weight_mu = saver_layer.weight_mu
            saver_weight_sigma = torch.log1p(torch.exp(saver_layer.weight_rho))

            if isinstance(trainer_layer, BayesianLinearVCL_bias):
                trainer_bias_mu = trainer_layer.bias_mu 
                saver_bias_mu = saver_layer.bias_mu
                trainer_bias_sigma = torch.log1p(torch.exp(trainer_layer.bias_rho))
                saver_bias_sigma = torch.log1p(torch.exp(saver_layer.bias_rho))
            
            mu_weight_reg = (torch.div((trainer_weight_mu-saver_weight_mu), saver_weight_sigma)).norm(2)**2
            mu_term = mu_term + mu_weight_reg

            weight_sigma = (trainer_weight_sigma**2 / saver_weight_sigma**2)
            sig_term = sig_term + (weight_sigma - 1).sum()

            log_sig_term = log_sig_term - weight_sigma.log().sum()
            if isinstance(trainer_layer, BayesianLinearVCL_bias):
                mu_bias_reg = (torch.div((trainer_bias_mu-saver_bias_mu), saver_bias_sigma)).norm(2)**2
                mu_term = mu_term + mu_bias_reg
                bias_sigma = (trainer_bias_sigma**2 / saver_bias_sigma**2)
                sig_term = sig_term + (bias_sigma - 1).sum()
                log_sig_term = log_sig_term - bias_sigma.log().sum()
            
        loss = loss + scale * (mu_term + sig_term + log_sig_term) * 0.5
        return loss


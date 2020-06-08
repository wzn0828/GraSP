import argparse
import json
import math
import os
import sys
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from models.model_base import ModelBase
from tensorboardX import SummaryWriter
from tqdm import tqdm
from models.base.init_utils import weights_init
from utils.common_utils import (get_logger, makedirs, process_config, PresetLRScheduler, str_to_list, accuracy, AverageMeter)
from utils.data_utils import get_dataloader
from utils.network_utils import get_network
from pruner.GraSP import GraSP


# Random seed, for deterministic behavior
import random
import torch.backends.cudnn as cudnn

cudnn.benchmark = False
cudnn.deterministic = True
random.seed(123)
torch.manual_seed(123)
torch.cuda.manual_seed_all(123)
# still need to set the work_init_fn to random.seed in train_dataloader, if multi numworkers



def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target_ratio', type=float)
    parser.add_argument('--iteration', type=int)
    parser.add_argument('--reset_to', type=int)

    parser.add_argument('--network', type=str, default='vgg')
    parser.add_argument('--dataset', type=str, default='cifar10')
    parser.add_argument('--depth', type=int, default=19)

    parser.add_argument('--pretrain_model', type=str)
    parser.add_argument('--log_dir', type=str, default='runs/')


def init_config():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str, required=False)
    parser.add_argument('--run', type=str, default='')
    args = parser.parse_args()


    os.environ['CUDA_VISIBLE_DEVICES'] = '3'
    args.config = '/home/wzn/PycharmProjects/GraSP/configs/tiny_imagenet/resnet32/GraSP_90.json'


    runs = None
    if len(args.run) > 0:
        runs = args.run
    config = process_config(args.config, runs)

    return config


def init_logger(config):
    makedirs(config.summary_dir)
    makedirs(config.checkpoint_dir)

    # set logger
    path = os.path.dirname(os.path.abspath(__file__))
    path_model = os.path.join(path, 'models/base/%s.py' % config.network.lower())
    path_main = os.path.join(path, 'main_prune_non_imagenet.py')
    path_pruner = os.path.join(path, 'pruner/%s.py' % config.pruner_file)
    logger = get_logger('log', logpath=config.summary_dir + '/',
                        filepath=path_model, package_files=[path_main, path_pruner])
    logger.info(dict(config))
    writer = SummaryWriter(config.summary_dir)
    # sys.stdout = open(os.path.join(config.summary_dir, 'stdout.txt'), 'w+')
    # sys.stderr = open(os.path.join(config.summary_dir, 'stderr.txt'), 'w+')
    return logger, writer


def print_mask_information(mb, logger):
    ratios = mb.get_ratio_at_each_layer()
    logger.info('** Mask information of %s. Overall Remaining: %.2f%%' % (mb.get_name(), ratios['ratio']))
    count = 0
    for k, v in ratios.items():
        if k == 'ratio':
            continue
        logger.info('  (%d) %s: Remaining: %.2f%%' % (count, k, v))
        count += 1


def save_state(net, acc, epoch, loss, config, ckpt_path, is_best=False):
    print('Saving..')
    state = {
        'net': net,
        'acc': acc,
        'epoch': epoch,
        'loss': loss,
        'args': config
    }
    if not is_best:
        torch.save(state, '%s/pruned_%s_%s%s_%d.t7' % (ckpt_path,
                                                       config.dataset,
                                                       config.network,
                                                       config.depth,
                                                       epoch))
    else:
        torch.save(state, '%s/finetuned_%s_%s%s_best.t7' % (ckpt_path,
                                                            config.dataset,
                                                            config.network,
                                                            config.depth))


def get_angular_loss(weight):
    '''
    :param weight: parameter of model, out_features *　in_features
    :return: angular loss
    '''
    # for convolution layers, flatten
    if weight.dim() > 2:
        weight = weight.view(weight.size(0), -1)

    # Dot product of normalized prototypes is cosine similarity.
    weight_ = F.normalize(weight, p=2, dim=1)
    product = torch.matmul(weight_, weight_.t())

    # Remove diagnonal from loss
    product_ = product - 2. * torch.diag(torch.diag(product))
    # Maxmize the minimum theta.
    loss = -torch.acos(product_.max(dim=1)[0].clamp(-0.99999, 0.99999)).mean()

    return loss


def train(net, loader, optimizer, criterion, lr_scheduler, epoch, writer, iteration):
    print('\nEpoch: %d' % epoch)
    net.train()
    train_loss = 0
    correct = 0
    total = 0
    top1 = AverageMeter()
    top5 = AverageMeter()

    lr_scheduler(optimizer, epoch)
    desc = ('[LR=%s] Loss: %.3f | Acc: %.3f%% (%d/%d)' %
            (lr_scheduler.get_lr(optimizer), 0, 0, correct, total))

    writer.add_scalar('iter_%d/train/lr' % iteration, lr_scheduler.get_lr(optimizer), epoch)

    prog_bar = tqdm(enumerate(loader), total=len(loader), desc=desc, leave=True)
    for batch_idx, (inputs, targets) in prog_bar:
        inputs, targets = inputs.cuda(), targets.cuda()
        optimizer.zero_grad()
        outputs = net(inputs)
        loss = criterion(outputs, targets)
        train_loss += loss.item()
        # import pdb; pdb.set_trace()

        for name, m in net.named_modules():
            if isinstance(m, (nn.Linear, nn.Conv2d)):
                angular_loss_hidden = get_angular_loss(m.weight)
                loss = loss + 0.02 * angular_loss_hidden

        loss.backward()
        optimizer.step()

        # _, predicted = outputs.max(1)
        # total += targets.size(0)
        # correct += predicted.eq(targets).sum().item()

        prec1, prec5 = accuracy(outputs.data, targets.data, topk=(1, 5))
        top1.update(prec1.item(), inputs.size(0))
        top5.update(prec5.item(), inputs.size(0))

        desc = ('[LR=%s] Loss: %.3f | Top1: %.3f 　Top5: %.3f' %
                (lr_scheduler.get_lr(optimizer), train_loss / (batch_idx + 1), top1.avg, top5.avg))
        prog_bar.set_description(desc, refresh=True)

    writer.add_scalar('iter_%d/train/loss' % iteration, train_loss / (batch_idx + 1), epoch)
    writer.add_scalar('iter_%d/TOP1/train' % iteration, top1.avg, epoch)
    writer.add_scalar('iter_%d/TOP5/train' % iteration, top5.avg, epoch)


def test(net, loader, criterion, epoch, writer, iteration):
    net.eval()
    test_loss = 0
    correct = 0
    total = 0
    desc = ('Loss: %.3f | Acc: %.3f%% (%d/%d)'
            % (test_loss / (0 + 1), 0, correct, total))

    prog_bar = tqdm(enumerate(loader), total=len(loader), desc=desc, leave=True)

    top1 = AverageMeter()
    top5 = AverageMeter()

    with torch.no_grad():
        for batch_idx, (inputs, targets) in prog_bar:
            inputs, targets = inputs.cuda(), targets.cuda()
            outputs = net(inputs)
            loss = criterion(outputs, targets)
            test_loss += loss.item()


            # _, predicted = outputs.max(1)
            # total += targets.size(0)
            # correct += predicted.eq(targets).sum().item()

            prec1, prec5 = accuracy(outputs.data, targets.data, topk=(1, 5))
            top1.update(prec1.item(), inputs.size(0))
            top5.update(prec5.item(), inputs.size(0))


            desc = ('Loss: %.3f | Top1: %.3f 　Top5: %.3f'
                    % (test_loss / (batch_idx + 1), top1.avg, top5.avg))
            prog_bar.set_description(desc, refresh=True)

    # Save checkpoint.
    acc = top1.avg

    writer.add_scalar('iter_%d/test/loss' % iteration, test_loss / (batch_idx + 1), epoch)
    writer.add_scalar('iter_%d/TOP1/test' % iteration, top1.avg, epoch)
    writer.add_scalar('iter_%d/TOP5/test' % iteration, top5.avg, epoch)

    return acc


def train_once(mb, net, trainloader, testloader, writer, config, ckpt_path, learning_rate, weight_decay, num_epochs,
               iteration, logger):
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.SGD(net.parameters(), lr=learning_rate, momentum=0.9, weight_decay=weight_decay)
    lr_schedule = {0: learning_rate,
                   int(num_epochs * 0.5): learning_rate * 0.1,
                   int(num_epochs * 0.75): learning_rate * 0.01}
    lr_scheduler = PresetLRScheduler(lr_schedule)
    best_acc = 0
    best_epoch = 0
    for epoch in range(num_epochs):
        train(net, trainloader, optimizer, criterion, lr_scheduler, epoch, writer, iteration=iteration)
        test_acc = test(net, testloader, criterion, epoch, writer, iteration)
        if test_acc > best_acc:
            print('Saving..')
            state = {
                'net': net,
                'acc': test_acc,
                'epoch': epoch,
                'args': config,
                # 'mask': mb.masks,
                # 'ratio': mb.get_ratio_at_each_layer()
            }
            path = os.path.join(ckpt_path, 'finetune_%s_%s%s_r%s_it%d_best.pth.tar' % (config.dataset,
                                                                                       config.network,
                                                                                       config.depth,
                                                                                       config.target_ratio,
                                                                                       iteration))
            torch.save(state, path)
            best_acc = test_acc
            best_epoch = epoch
    logger.info('Iteration [%d], best acc: %.4f, epoch: %d' %
                (iteration, best_acc, best_epoch))


def get_exception_layers(net, exception):
    exc = []
    idx = 0
    for m in net.modules():
        if isinstance(m, (nn.Linear, nn.Conv2d)):
            if idx in exception:
                exc.append(m)
            idx += 1
    return tuple(exc)


def main(config):
    # init logger
    classes = {
        'cifar10': 10,
        'cifar100': 100,
        'mnist': 10,
        'tiny_imagenet': 200
    }
    logger, writer = init_logger(config)

    # build model
    model = get_network(config.network, config.depth, config.dataset, use_bn=config.get('use_bn', True))
    mask = None
    mb = ModelBase(config.network, config.depth, config.dataset, model)
    mb.cuda()
    if mask is not None:
        mb.register_mask(mask)
        print_mask_information(mb, logger)

    # preprocessing
    # ====================================== get dataloader ======================================
    trainloader, testloader = get_dataloader(config.dataset, config.batch_size, 256, 4, root='/home/wzn/PycharmProjects/GraSP/data')
    # ====================================== fetch configs ======================================
    ckpt_path = config.checkpoint_dir
    num_iterations = config.iterations
    target_ratio = config.target_ratio
    normalize = config.normalize
    # ====================================== fetch exception ======================================
    # exception = get_exception_layers(mb.model, str_to_list(config.exception, ',', int))
    # logger.info('Exception: ')
    #
    # for idx, m in enumerate(exception):
    #     logger.info('  (%d) %s' % (idx, m))

    # ====================================== fetch training schemes ======================================
    ratio = 1 - (1 - target_ratio) ** (1.0 / num_iterations)
    learning_rates = str_to_list(config.learning_rate, ',', float)
    weight_decays = str_to_list(config.weight_decay, ',', float)
    training_epochs = str_to_list(config.epoch, ',', int)
    logger.info('Normalize: %s, Total iteration: %d, Target ratio: %.2f, Iter ratio %.4f.' %
                (normalize, num_iterations, target_ratio, ratio))
    logger.info('Basic Settings: ')
    for idx in range(len(learning_rates)):
        logger.info('  %d: LR: %.5f, WD: %.5f, Epochs: %d' % (idx,
                                                              learning_rates[idx],
                                                              weight_decays[idx],
                                                              training_epochs[idx]))

    # ====================================== start pruning ======================================
    iteration = 0
    for _ in range(1):
        # logger.info('** Target ratio: %.4f, iter ratio: %.4f, iteration: %d/%d.' % (target_ratio,
        #                                                                             ratio,
        #                                                                             1,
        #                                                                             num_iterations))

        mb.model.apply(weights_init)
        print("=> Applying weight initialization(%s)." % config.get('init_method', 'kaiming'))


        # print("Iteration of: %d/%d" % (iteration, num_iterations))
        # masks = GraSP(mb.model, ratio, trainloader, 'cuda',
        #               num_classes=classes[config.dataset],
        #               samples_per_class=config.samples_per_class,
        #               num_iters=config.get('num_iters', 1))
        # iteration = 0
        # print('=> Using GraSP')
        # # ========== register mask ==================
        # mb.register_mask(masks)
        # # ========== save pruned network ============
        # logger.info('Saving..')
        # state = {
        #     'net': mb.model,
        #     'acc': -1,
        #     'epoch': -1,
        #     'args': config,
        #     'mask': mb.masks,
        #     'ratio': mb.get_ratio_at_each_layer()
        # }
        # path = os.path.join(ckpt_path, 'prune_%s_%s%s_r%s_it%d.pth.tar' % (config.dataset,
        #                                                                    config.network,
        #                                                                    config.depth,
        #                                                                    config.target_ratio,
        #                                                                    iteration))
        # torch.save(state, path)

        # # ========== print pruning details ============
        # logger.info('**[%d] Mask and training setting: ' % iteration)
        # print_mask_information(mb, logger)
        # logger.info('  LR: %.5f, WD: %.5f, Epochs: %d' %
        #             (learning_rates[iteration], weight_decays[iteration], training_epochs[iteration]))

        # ========== finetuning =======================
        train_once(mb=mb,
                   net=mb.model,
                   trainloader=trainloader,
                   testloader=testloader,
                   writer=writer,
                   config=config,
                   ckpt_path=ckpt_path,
                   learning_rate=learning_rates[iteration],
                   weight_decay=weight_decays[iteration],
                   num_epochs=training_epochs[iteration],
                   iteration=iteration,
                   logger=logger)


if __name__ == '__main__':
    config = init_config()
    main(config)


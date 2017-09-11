# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the LICENSE file in
# the root directory of this source tree. An additional grant of patent rights
# can be found in the PATENTS file in the same directory.
#

"""
Train a network on multiple GPUs using multiprocessing.
"""

import math
import torch
from torch.optim.lr_scheduler import LambdaLR, ReduceLROnPlateau

from fairseq import nccl, utils
from fairseq.multiprocessing_event_loop import MultiprocessingEventLoop, Future
from fairseq.nag import NAG


class MultiprocessingTrainer(MultiprocessingEventLoop):
    """Main class for multi-GPU training.

    Each GPU has a full copy of the model and is assigned to its own Python
    process. Gradients are accumulated with all-reduce and all model replicas
    are updated synchronously after each batch.

    The methods in this class are divided into synchronous functions, which
    prepare and dispatch the input to each process, and asynchronous functions
    (prefixed with `_async_`), which run on each process in parallel.
    """

    def __init__(self, args, model, device_ids=None,
                 multiprocessing_method='spawn'):
        if device_ids is None:
            device_ids = tuple(range(torch.cuda.device_count()))
        super().__init__(device_ids, multiprocessing_method)

        if not torch.cuda.is_available():
            raise NotImplementedError('Training on CPU is not supported')
        model = model.share_memory()
        nccl_uid = nccl.get_unique_id()

        Future.gen_list([
            self.call_async(rank, '_async_init', args=args, model=model,
                            nccl_uid=nccl_uid)
            for rank in range(self.num_replicas)
        ])

    def _async_init(self, rank, device_id, args, model, nccl_uid):
        """Initialize child processes."""
        self.args = args

        # set CUDA device
        torch.cuda.set_device(device_id)

        # initialize NCCL
        nccl.initialize(self.num_replicas, nccl_uid, device_id)

        # copy model to current device
        self.model = model.cuda()

        # initialize optimizer
        self.optimizer = NAG(self.model.parameters(), lr=self.args.lr,
                             momentum=self.args.momentum,
                             weight_decay=self.args.weight_decay)
        self.flat_grads = None

        # initialize LR scheduler
        self.lr_scheduler = self._build_lr_scheduler()

    def _build_lr_scheduler(self):
        if self.args.force_anneal > 0:
            def anneal(e):
                if e < self.args.force_anneal:
                    return 1
                else:
                    return self.args.lrshrink ** (e + 1 - self.args.force_anneal)
            lr_scheduler = LambdaLR(self.optimizer, anneal)
            lr_scheduler.best = None
        else:
            # decay the LR by 0.1 every time the validation loss plateaus
            lr_scheduler = ReduceLROnPlateau(self.optimizer, patience=0)
        return lr_scheduler


    def get_model(self):
        """Get one of the model replicas."""
        # just return the first model, since all replicas are the same
        return self.call_async(0, '_async_get_model').gen()

    def _async_get_model(self, rank, device_id):
        return self.model


    def save_checkpoint(self, args, epoch, batch_offset, val_loss=None):
        """Save a checkpoint for the current model."""
        self.call_async(0, '_async_save_checkpoint', args=args, epoch=epoch,
                        batch_offset=batch_offset, val_loss=val_loss).gen()

    def _async_save_checkpoint(self, rank, device_id, args, epoch, batch_offset, val_loss):
        utils.save_checkpoint(args, epoch, batch_offset, self.model,
                              self.optimizer, self.lr_scheduler, val_loss)


    def load_checkpoint(self, filename):
        """Load a checkpoint into the model replicas in each process."""
        results = Future.gen_list([
            self.call_async(rank, '_async_load_checkpoint', filename=filename)
            for rank in range(self.num_replicas)
        ])
        epoch, batch_offset = results[0]
        return epoch, batch_offset

    def _async_load_checkpoint(self, rank, device_id, filename):
        return utils.load_checkpoint(filename, self.model, self.optimizer,
                                     self.lr_scheduler, cuda_device=device_id)


    def train_step(self, sample):
        """Do forward, backward and gradient step in parallel."""
        # scatter sample across GPUs
        net_inputs, data_events = self._scatter_sample(sample)
        ntokens = sum(s['ntokens'] if s else 0 for s in sample)

        # forward pass, backward pass and gradient step
        losses = [
            self.call_async(rank, '_async_train_step',
                            net_input=input['net_input'] if input else None,
                            grad_denom=ntokens, data_event=event)
            for rank, (input, event) in enumerate(zip(net_inputs, data_events))
        ]

        # accumulate and normalize loss
        losses, grad_norms = Future.gen_tuple_list(losses)
        loss = sum(losses) / ntokens

        return loss / math.log(2), grad_norms[0]

    def _async_train_step(self, rank, device_id, net_input, grad_denom, data_event):
        data_event.wait()
        self.model.train()

        # zero grads even if net_input is None, since we will all-reduce them
        self.optimizer.zero_grad()

        # calculate loss and grads
        loss = 0
        if net_input is not None:
            loss_ = self.model(**net_input)
            loss_.backward()
            loss = loss_.data[0]

        # flatten grads into a contiguous block of memory
        if self.flat_grads is None:
            self.flat_grads = self._flatten_grads_(self.model)

        # all-reduce grads
        nccl.all_reduce(self.flat_grads)

        # normalize and clip grads
        self.flat_grads.div_(grad_denom)
        grad_norm = self._clip_grads_(self.flat_grads, self.args.clip_norm)

        # take an optimization step
        self.optimizer.step()

        return loss, grad_norm

    def _flatten_grads_(self, model):
        num_params = sum(p.data.numel() for p in model.parameters())
        flat_grads = next(model.parameters()).data.new(num_params)
        offset = 0
        for p in model.parameters():
            grad = p.grad.data
            numel, sz = grad.numel(), grad.size()
            flat_grads[offset:offset+numel] = grad.view(-1)
            grad.set_(flat_grads[offset:offset+numel])
            grad.resize_(sz)  # preserve original shape
            offset += numel
        return flat_grads

    def _clip_grads_(self, flat_grads, clipv):
        norm = flat_grads.norm()
        if clipv > 0 and norm > clipv:
            coef = max(norm, 1e-6) / clipv
            flat_grads.div_(coef)
        return norm


    def valid_step(self, sample):
        """Do forward pass in parallel."""
        # forward pass
        net_inputs, data_events = self._scatter_sample(sample, volatile=True)
        losses = [
            self.call_async(rank, '_async_valid_step',
                            net_input=input['net_input'] if input else None, data_event=event)
            for rank, (input, event) in enumerate(zip(net_inputs, data_events))
        ]

        # accumulate and normalize loss
        ntokens = sum(s['ntokens'] if s else 0 for s in sample)
        loss = sum(Future.gen_list(losses)) / ntokens

        return loss / math.log(2)

    def _async_valid_step(self, rank, device_id, net_input, data_event):
        if net_input is None:
            return 0
        data_event.wait()

        self.model.eval()
        loss = self.model(**net_input)
        return loss.data[0]


    def get_lr(self):
        """Get the current learning rate."""
        return self.call_async(0, '_async_get_lr').gen()

    def _async_get_lr(self, rank, device_id):
        return self.optimizer.param_groups[0]['lr']


    def lr_step(self, val_loss=None, epoch=None):
        """Adjust the learning rate depending on the validation loss."""
        lr = Future.gen_list([
            self.call_async(rank, '_async_lr_step', val_loss=val_loss, epoch=epoch)
            for rank in range(self.num_replicas)
        ])
        return lr[0]

    def _async_lr_step(self, rank, device_id, epoch, val_loss):
        # update the learning rate
        if self.args.force_anneal > 0:
            self.lr_scheduler.step(epoch)
        else:
            self.lr_scheduler.step(val_loss, epoch)
        return self.optimizer.param_groups[0]['lr']


    def _scatter_sample(self, sample, volatile=False):
        """Split and distribute a sample across GPUs."""
        # prepare input on CPU and let scatter move it to GPU
        # returned list may be smaller than the number of available devices
        res = [utils.prepare_sample(sample[i], volatile=volatile,
                                    cuda_device=device_id)
               for i, device_id in zip(range(len(sample)), self.device_ids)]

        # Synchronize GPU devices after data is sent to prevent
        # race conditions.
        events = []
        for d in self.device_ids:
            with torch.cuda.device(d):
                event = torch.cuda.Event(interprocess=True)
                event.record()
                events.append(event)

        return res + [None]*(self.num_replicas - len(sample)), events
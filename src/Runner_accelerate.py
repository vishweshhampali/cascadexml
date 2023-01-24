import torch
import torch.nn as nn
import numpy as np
import scipy.sparse as sp

from transformers import AdamW
from tqdm import tqdm
from torch.cuda.amp import GradScaler
import torch.distributed as dist
from training_schedule import ThreePhaseOneCycleLR
import os
import time
from accelerate import Accelerator

class Runner:
    def __init__(self, params, train_dl, test_dl, inv_prop, top_k=5):
        self.train_dl = train_dl
        self.test_dl = test_dl
        self.num_train, self.num_test = len(train_dl.dataset), len(test_dl.dataset)
        self.top_k = top_k
        self.update_count = params.update_count
        self.inv_prop = torch.from_numpy(inv_prop).double()

    def save_model(self, model, epoch, name):
        checkpoint = {
            'state_dict': model.state_dict(),
            'optimizer': self.optimizer.state_dict(),
            'epoch': epoch,
            'scaler': self.scaler.state_dict()
        }
        torch.save(checkpoint, name)

    def load_model(self, model, name, load_opt=True):
        checkpoint = torch.load(name)
        try:
            model.load_state_dict(checkpoint['state_dict'], strict=False)
        except RuntimeError as E:
            print(E)

        if load_opt:
            self.optimizer.load_state_dict(checkpoint['optimizer'])
            self.scaler.load_state_dict(checkpoint["scaler"])
        init = checkpoint['epoch']
        return model, init
    
    def get_recall(self, preds, y_true, i, ext = None):
        for pred, tr in zip(preds, y_true):
            match = (pred[..., None] == tr).any(-1)
            self.recall[i] += torch.sum(match)/len(tr)

    def predict(self, preds, y_true, i, ext = None):
        ext = self.counts if ext is None else ext
        for pred, tr in zip(preds, y_true):
            match = (pred[..., None] == tr).any(-1)
            ext[i] += torch.cumsum(match, dim=0).to(ext[i].device)
        
    def psp(self, preds, y_true, num=None, den=None):
        num = self.num if num is None else num
        den = self.den if den is None else den
        for pred, tr in zip(preds, y_true):
            match = (pred[..., None] == tr).any(-1).double()
            match[match > 0] = self.inv_prop[pred[match > 0]]
            num += torch.cumsum(match, dim=0).to(num.device)

            inv_prop_sample = torch.sort(self.inv_prop[tr], descending=True)[0]

            match = torch.zeros(self.top_k)
            match_size = min(tr.shape[0], self.top_k)
            match[:match_size] = inv_prop_sample[:match_size]
            den += torch.cumsum(match, dim=0).to(den.device)

    def fit_one_epoch(self, model, accelerator, params, device, epoch):
        trainLoss = torch.tensor(0.0).to(device)
        #Do we need this model.module crap now? 
        breakpoint()
        if isinstance(model,  nn.parallel.DistributedDataParallel):
            self.counts = [torch.zeros(self.top_k, dtype=np.int).to(device) for _ in range(len(model.module.clusters)+1)]
        else:
            self.counts = [torch.zeros(self.top_k, dtype=np.int).to(device) for _ in range(len(model.clusters)+1)]

        model.train()
        len_dl = len(self.train_dl)

        self.optimizer.zero_grad()

        pbar = tqdm(self.train_dl, desc=f"Epoch {epoch}", disable=not accelerator.is_main_process)

        # if params.local_rank==0:
        #     print(f'\nStarting Epoch: {epoch}\n')
        #     pbar = tqdm(self.train_dl, desc=f"Epoch {epoch}")
        # else:
        #     pbar = self.train_dl

        st = time.time()
        for step, sample in enumerate(pbar):
            with self.accelerator.accumulate(model):
                # x_batch, attention_mask, labels = sample[0].to(device), sample[1].to(device), sample[2]
                x_batch, attention_mask, labels = sample[0], sample[1], sample[2]
                
                all_probs, all_candidates, loss = model(x_batch, attention_mask, epoch, labels)
                self.accelerator.backward(loss)

                if not params.distributed:
                    if (step + 1) % self.update_count == 0:
                        # self.scaler.unscale_(self.optimizer)
                        # nn.utils.clip_grad_norm_(model.parameters(), 1.0)

                        self.optimizer.step()
                        self.optimizer.zero_grad()
                        self.cycle_scheduler.step()
                else:
                    self.optimizer.step()
                    self.optimizer.zero_grad()
                    self.cycle_scheduler.step()

            trainLoss += loss.item()

            all_preds = [torch.topk(probs, self.top_k)[1].cpu() for probs in all_probs]

            if all_candidates is not None:
                all_preds = [
                    candidates[np.arange(preds.shape[0]).reshape(-1, 1), preds] #.cpu()
                    for candidates, preds in zip(all_candidates, all_preds)
                ]

            for i, (preds, label) in enumerate(zip(all_preds, labels)):
                self.predict(preds, label, i)
            
        print(f"Train time : {time.time() - st} seconds")
        if params.distributed:
            # dist.barrier()
            dist.reduce(trainLoss, dst=0, op=dist.ReduceOp.SUM)
            for c in self.counts:
                dist.reduce(c, dst=0, op=dist.ReduceOp.SUM)

        if params.local_rank == 0:
            trainLoss /= len_dl
            precs = [count.detach().cpu().numpy() * 100.0 / (self.num_train * np.arange(1, self.top_k+1)) for count in self.counts]

            print(f"Epoch: {epoch},  Train Loss: {trainLoss.item()}")
            print("Grouped LRs: ", [param_group['lr'] for param_group in self.optimizer.param_groups])
            for i in range(len(precs)):
                print(f'Level-{i} Training Scores: P@1: {precs[i][0]:.2f}, P@3: {precs[i][2]:.2f}, P@5: {precs[i][4]:.2f}')
            print()
        
        if params.distributed:
            dist.barrier()

        # if epoch == 7:
        #     self.save_model(model, epoch, params.model_name + f"/model_{epoch}.pth")
        
        if epoch > 7:
            self.test(model, params, device, epoch)

    def train(self, model, params, accelerator):
        # test only on one process
        self.best_train_Loss = float('Inf')
        self.best_test_acc = 0
        lr = params.lr

        # if params.distributed:
        #     steps_per_epoch = len(self.train_dl)
        # else:
        #     steps_per_epoch = len(self.train_dl)//4+1

        device = accelerator.device
        model.to(device)

        no_decay = ['bias', 'LayerNorm.weight']
        wd = params.weight_decay 
        optimizer_grouped_parameters = [
            {'params': [p for n, p in model.bert.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': wd, 'lr': lr},
            {'params': [p for n, p in model.bert.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0, 'lr': lr},
            {'params': [p for n, p in [*model.Cn_hidden.named_parameters(), *model.Cn.named_parameters()] 
                                            if not any(nd in n for nd in no_decay)], 'weight_decay': wd/10, 'lr': lr*10},
            {'params': [p for n, p in [*model.Cn_hidden.named_parameters()] if any(nd in n for nd in no_decay)] + 
                                                [p for n, p in [*model.Cn_bias.named_parameters()]], 'weight_decay': 0.0, 'lr': lr*10}
        ]
        
        optimizer = AdamW(optimizer_grouped_parameters, lr = lr)

        if params.gradient_checkpointing:
            model.bert.gradient_checkpointing_enable()

        cycle_scheduler = ThreePhaseOneCycleLR(
            optimizer=self.optimizer, max_lr=[lr, lr, lr*10, lr*10], epochs=params.num_epochs, 
            steps_per_epoch=steps_per_epoch, pct_epoch=[params.warmup, params.num_epochs-3],
            div_factor=10, final_div_factor=100, last_epoch=last_batch, three_phase=True)

        model, self.optimizer, self.train_dl, self.cycle_scheduler = accelerator.prepare(model, optimizer, self.train_dl, cycle_scheduler)
        
        init = 0
        last_batch = -1
        
        if len(params.load_model):
            print("Loading model from ", params.load_model)
            load_opt = not (params.return_embeddings or params.test)
            model, init = self.load_model(model, params.load_model, load_opt)
            last_batch = init*(len(self.train_dl)//4)
        
        if params.return_embeddings:
            self.return_embeddings(model, params)

        if params.return_shortlist:
            self.return_shortlist(model, params)

        if params.test:
            self.test(model, params, device, init)
            return

        if accelerator.is_main_process:
            print("Starting LRs per group: ", [param_group['lr'] for param_group in self.optimizer.param_groups])

        accelerator.wait_for_everyone()

        for epoch in range(init, params.num_epochs):
            self.fit_one_epoch(model, accelerator, params, device, epoch+1)

    @torch.no_grad()
    def test(self, model, params, device, epoch=0):
        if not params.dist_eval and params.local_rank != 0:
            dist.barrier()  # params.distributed will always be true here
            return
        
        model.eval()
        if isinstance(model,  nn.parallel.DistributedDataParallel):
            self.counts = [torch.zeros(self.top_k, dtype=np.int).to(device) for _ in range(len(model.module.clusters)+1)]
            self.weighted_counts = [torch.zeros(self.top_k, dtype=np.int).to(device) for _ in range(len(model.module.clusters)+1)]
        else:
            self.recall = torch.zeros(len(model.clusters)+1, dtype=np.float).to(device)
            self.counts = [torch.zeros(self.top_k, dtype=np.int).to(device) for _ in range(len(model.clusters)+1)]
            self.weighted_counts = [torch.zeros(self.top_k, dtype=np.int).to(device) for _ in range(len(model.clusters)+1)]
        
        self.num = torch.zeros(self.top_k).cuda()
        self.den = torch.zeros(self.top_k).cuda()

        if params.local_rank==0:
            pbar = tqdm(self.test_dl, desc=f"Epoch {epoch}")
        else:
            pbar = self.test_dl

        idx, cands, cprobs = [], [], []

        for step, sample in enumerate(pbar): 
            x_batch, attention_mask, labels = sample[0].to(device), sample[1].to(device), sample[2]

            with torch.cuda.amp.autocast():
                all_probs, all_candidates, all_probs_weighted = model(x_batch, attention_mask, epoch)

            all_recall = [torch.topk(probs, 512)[1].cpu() for probs in all_probs]
            all_preds = [torch.topk(probs, self.top_k)[1].cpu() for probs in all_probs]
            all_weighted_preds = [torch.topk(probs, self.top_k)[1].cpu() for probs in all_probs_weighted]

            if all_candidates is not None:
                all_recall = [candidates[np.arange(preds.shape[0]).reshape(-1, 1), preds].cpu()
                    for candidates, preds in zip(all_candidates, all_recall)]

                all_preds = [candidates[np.arange(preds.shape[0]).reshape(-1, 1), preds].cpu()
                    for candidates, preds in zip(all_candidates, all_preds)]

                all_weighted_preds = [candidates[np.arange(preds.shape[0]).reshape(-1, 1), preds].cpu()
                    for candidates, preds in zip(all_candidates, all_weighted_preds)]

            # if params.eval_scheme == "weighted":
            #     ens_probs, ens_cand = torch.topk(all_probs_weighted[-1], 20)
            # else:
            #     ens_probs, ens_cand = torch.topk(all_probs[-1], 20)
            # ens_cand = all_candidates[-1][np.arange(ens_cand.shape[0]).reshape(-1, 1), ens_cand]

            # idx.append(torch.repeat_interleave(torch.arange(ens_cand.shape[0]), ens_cand.shape[1]) + step*params.batch_size)
            # cands.append(torch.flatten(ens_cand).detach().cpu())
            # cprobs.append(torch.flatten(ens_probs).detach().cpu())

            for i, (preds, w_preds, r_preds, label) in enumerate(zip(all_preds, all_weighted_preds, all_recall, labels)):
                self.predict(preds, label, i)
                self.get_recall(r_preds, label, i)
                if i > 0:
                    self.predict(w_preds, label, i, self.weighted_counts)
            
            if params.eval_scheme == "weighted":
                self.psp(all_weighted_preds[-1], labels[-1])
            else:
                self.psp(all_preds[-1], labels[-1])
            # pbar.set_postfix({'group_counts': self.group_count.tolist(), 'extreme_counts': self.extreme_count.tolist()})

        if params.dist_eval:
            dist.barrier()
            for c, wc in zip(self.counts, self.weighted_counts):
                dist.reduce(c, dst=0, op=dist.ReduceOp.SUM)
                dist.reduce(wc, dst=0, op=dist.ReduceOp.SUM)

        if params.local_rank == 0:
            recall = self.recall.detach().cpu().numpy() * 100.0 / self.num_test
            precs = [count.detach().cpu().numpy() * 100.0 / (self.num_test * np.arange(1, self.top_k+1)) for count in self.counts]
            weighted_precs = [count.detach().cpu().numpy() * 100.0 / (self.num_test * np.arange(1, self.top_k+1)) for count in self.weighted_counts]
            psp = (self.num * 100 / self.den).detach().cpu().numpy()

            for i in range(len(precs)):
                print(f'Level-{i} Test Recall: P@1: {recall[i]:.2f}')
                print(f'Level-{i} Test Scores: P@1: {precs[i][0]:.2f}, P@3: {precs[i][2]:.2f}, P@5: {precs[i][4]:.2f}')
                if i != 0:
                    print(f'Level-{i} Weighted Test Scores: P@1: {weighted_precs[i][0]:.2f}, P@3: {weighted_precs[i][2]:.2f}, P@5: {weighted_precs[i][4]:.2f}')
            print(f"Level-{i} Weighted PSP Score: PSP@1: {psp[0]:.2f}, PSP@3: {psp[2]:.2f}, PSP@5: {psp[4]:.2f}")
            
            # idx = torch.concat(idx, dim=0)
            # cands = torch.concat(cands, dim=0)
            # cprobs = torch.concat(cprobs, dim=0)

            # logits = sp.csr_matrix((cprobs, (idx, cands)), (len(self.test_dl.dataset), params.num_labels+1))
            # sp.save_npz(f'./{params.dataset}_{params.bert}_{params.seed}_preds.npz', logits)
            
            if params.eval_scheme == "weighted":
                score = weighted_precs[-1][0]+weighted_precs[-1][2]+weighted_precs[-1][4]
            else:
                score = precs[-1][0]+precs[-1][2]+precs[-1][4]

            if(score > self.best_test_acc and not params.test):
                self.best_test_acc = score
                self.save_model(model, epoch, params.model_name + "/model_best_test.pth")
            
        if params.dist_eval:
            dist.barrier()

    # @staticmethod
    def test_ensemble(self, params):
        
        self.counts = [torch.zeros(self.top_k, dtype=np.int)]
        self.num = torch.zeros(self.top_k)
        self.den = torch.zeros(self.top_k)
        
        ensemble_cands = []
        for fil in params.ensemble_files:
            if fil[-4:] == '.txt':
                with open(fil) as f:
                    row_idx, col_idx, val_idx = [], [], []
                    predictions = f.readlines()
                    predictions = predictions[1:]
                    for i, scores in enumerate(predictions):
                        scores = scores.replace('\n', '').split()
                        labels = [int(x.split(':')[0]) for x in scores]
                        lab_scores = torch.tensor([float(x.split(':')[1]) for x in scores]).sigmoid()
                        for l, s in zip(labels, lab_scores):
                            row_idx.append(i)
                            col_idx.append(l)
                            val_idx.append(s)
                    m = max(row_idx) + 1
                    n = params.num_labels
                    Y = sp.csr_matrix((val_idx, (row_idx, col_idx)), shape=(m, n))
                    sp.save_npz(os.path.splitext(fil)[0]+'.npz', Y)
            else:
                Y = sp.load_npz(fil)
            ensemble_cands.append(Y)

        for i, batch_data in enumerate(tqdm(self.test_dl, desc=f"Testing Ensemble")):
            y_tr = batch_data[2]
            
            logits = ensemble_cands[0][i*params.batch_size : (i+1)*params.batch_size]
            for result in ensemble_cands[1:]:
                logits += result[i*params.batch_size : (i+1)*params.batch_size]

            logits = torch.FloatTensor(logits.todense())
            preds = torch.topk(logits, self.top_k)[1]
            self.predict(preds, y_tr[-1], 0)
            self.psp(preds, y_tr[-1])
            
        prec = self.counts[0].numpy() * 100.0 / (self.num_test * np.arange(1, self.top_k+1))
        psp = (self.num * 100 / self.den).numpy()

        print(f"Test scores: P@1: {prec[0]:.2f}, P@3: {prec[2]:.2f}, P@5: {prec[4]:.2f}\n")
        print(f"Test Propensity scores: PSP@1: {psp[0]:.2f}, PSP@3: {psp[2]:.2f}, PSP@5: {psp[4]:.2f}\n")
        exit()
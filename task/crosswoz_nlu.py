import os
import sys
import logging
import numpy as np
import json
import re
import random
from tqdm import tqdm

word_dir = os.getcwd()
sys.path.extend([os.path.abspath(".."), word_dir])

from basic.basic_task import Basic_task, Task_Mode
from basic.register import register_task, find_task
from utils.build_vocab import Vocab
from utils.utils import check_dir, calculateF1

import torch
from torch import nn
from TorchCRF import CRF
from transformers import BertPreTrainedModel, BertConfig, BertTokenizer, BertModel
from utils.ner_metrics import SeqEntityScore
import matplotlib.pyplot as plt

logging.basicConfig(format='%(asctime)s:%(levelname)s: %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

workdir = os.getcwd()  # 当前路径
project_dir = os.path.split(workdir)[0]
"""
Crosswoz NLU任务
数据集：Crosswoz数据
    -训练集：5012个对话，84692个utterance（句子）
    -验证集：500个对话，8458个utterance（句子）
    -测试集：500个对话，8476个utterance（句子）
槽位提取模型：bert + Bilstm + crf  
intent 模型：bert + mean pool + linear + sigmoid

intent识别效果
    验证集： p = 0.9865, r = 0.9042, f1 = 0.9435
    测试集： p = 0.9859, r = 0.8976, f1 = 0.9397

slot提取效果：
    验证集： acc = 0.9658 ,  recall = 0.9794 ,  f1 = 0.9726 
    测试集： acc = 0.9637 ,  recall = 0.9798 ,  f1 = 0.9717

"""
class Config:
    seed = 42   # 随机种子
    gpuids = "1"  # 设置显卡序号，若为None，则不使用gpu
    nlog = 100  # 多少step打印一次记录（loss，评估指标）
    neval = 300
    early_stop = True

    train_batch_size = 32
    eval_batch_size = 32
    epochs = 5
    lr = 5e-5   # 学习率

    do_train = True
    do_eval = True
    do_infer = False

    # 新增超参数
    margin = 1
    max_len = 128
    hidden_units = 128
    num_labels = 12
    use_lstm = True

    task_name = "Crosswoz_nlu"

    # 配置路径
    train_data_path = "/workspace/crosswoz_nlu/data/crosswoz/nlu/train_nlu.json"  # 训练集数据的路径，建议绝对路径
    dev_data_path = ["/workspace/crosswoz_nlu/data/crosswoz/nlu/val_nlu.json", "/workspace/crosswoz_nlu/data/crosswoz/nlu/test_nlu.json"] # 验证集数据的路径，建议绝对路径
    test_data_path = ["/workspace/crosswoz_nlu/data/crosswoz/nlu/test_nlu.json"]  # 测试集数据的路径，建议绝对路径

    # transformer结构(Bert, Albert, Roberta等)的预训练模型的配置, 路径也建议是绝对路径
    bert_model_path = "/workspace/Idiom_cloze/pretrained_models/chinese_wwm_pytorch/pytorch_model.bin"  # 预训练模型路径， 例如bert预训练模型
    model_config_path = "/workspace/Idiom_cloze/pretrained_models/chinese_wwm_pytorch/config.json"  # 预训练模型的config文件路径， 一般是json文件
    vocab_path = "/workspace/Idiom_cloze/pretrained_models/chinese_wwm_pytorch/vocab.txt"  # vocab文件路径，可以是预训练模型的vocab.txt文件

    model_save_path = project_dir + f"/model_save/{task_name.lower()}_model"  # 训练过程中最优模型或者训练结束后的模型保存路径
    output_path = project_dir + f"/output/{task_name.lower()}_model"  # 模型预测输出预测结果文件的路径

    # 新增文件路径
    slot_vocab_path = "/workspace/crosswoz_nlu/data/crosswoz/nlu/slots_vocab.txt"
    intent_vocab_path = "/workspace/crosswoz_nlu/data/crosswoz/nlu/intents_vocab.txt"


# 构建模型动态计算图
class Model(BertPreTrainedModel):
    """
    模型说明：bert + Bilstm + crf
    """
    def __init__(self, model_config, task_config):
        super(Model, self).__init__(model_config)
        # 768 is the dimensionality of bert-base-uncased's hidden representations
        # Load the pretrained BERT model
        self.model_config = model_config
        self.task_config = task_config
        self.bert = BertModel(config=model_config)
        self.lstm = nn.LSTM(model_config.hidden_size, task_config.hidden_units, num_layers=1, bidirectional=True, batch_first=True)
        self.dropout = nn.Dropout(0.5)
        self.intent_classifier = nn.Linear(model_config.hidden_size, task_config.intent_num_labels)
        self.slot_classifier = nn.Linear(task_config.hidden_units * 2, task_config.slot_num_labels)
        self.crf = CRF(task_config.slot_num_labels, use_gpu=True)

        self.init_weights()

    def forward(self, inputs):
        
        input_ids = inputs.get("input_ids", None)
        attention_mask = inputs.get("input_masks", None)
        token_type_ids = inputs.get("token_type_ids", None)
        intent_ids = inputs.get("intent_ids", None)
        slot_ids = inputs.get("slot_ids", None)
        intent_weights = inputs.get("intent_weights", None)

        # input_ids [batch, max_seq_length]  sequence_outputs [batch, max_seq_length, hidden_state]
        bert_outputs = self.bert(input_ids, attention_mask, token_type_ids)
        sequence_outputs = bert_outputs[0]
        pooled_output = bert_outputs[1]
        # intent 分类
        mean_pool_output = torch.mean(sequence_outputs * attention_mask.unsqueeze(2), dim=1)
        mean_pool_output_drop = self.dropout(mean_pool_output)
        intent_logits = self.intent_classifier(mean_pool_output_drop)
        # slot
        if self.task_config.use_lstm:
            bilstm_outputs, _ = self.lstm(sequence_outputs)
        bilstm_outputs_drop = self.dropout(bilstm_outputs)
        emissions = self.slot_classifier(bilstm_outputs_drop)
        slot_logits = self.crf.viterbi_decode(emissions, attention_mask.byte())
        outputs = {
            "intent_logits": intent_logits,
            "slot_logits": slot_logits,
        }
        if intent_ids is not  None:
            loss_fn = nn.BCEWithLogitsLoss(pos_weight=intent_weights)  # pos_weight=intent_weights
            intent_loss = loss_fn(intent_logits, intent_ids.float()) 
            outputs["intent_loss"] = intent_loss
            outputs["loss"] = intent_loss
        if slot_ids is not  None:
            slot_loss = -1 * self.crf(emissions, slot_ids, mask=attention_mask.byte())
            slot_loss = slot_loss.mean()
            outputs["slot_loss"] = slot_loss
            outputs["loss"] += slot_loss
        
        return outputs

# 编写任务
@ register_task
class Crosswoz_nlu(Basic_task):
    def __init__(self, task_config):
        super().__init__(task_config)
        self.task_config = task_config
        self.max_len = task_config.max_len
        # model init 模型初始化，加载预训练模型
        self.model_config = BertConfig.from_pretrained(self.task_config.model_config_path)

        self.vocab = Vocab(task_config.vocab_path)
        self.slot_vocab = Vocab(self.task_config.slot_vocab_path)
        self.intent_vocab = Vocab(self.task_config.intent_vocab_path)
        self.intent_weights = np.ones((self.intent_vocab.vocab_size), dtype=np.float)
       
        self.task_config.intent_num_labels = self.intent_vocab.vocab_size
        self.task_config.slot_num_labels = self.slot_vocab.vocab_size
        if task_config.do_train:
            self.model = Model.from_pretrained(pretrained_model_name_or_path=self.task_config.bert_model_path,
                                           config=self.model_config, task_config=self.task_config)
        else:
            self.model = Model(self.model_config, task_config=self.task_config)

        if self.task_config.gpuids != None:
            self.model.to(self.device)
        # 单机多卡训练
        if self.n_gpu > 1:
            self.model = nn.DataParallel(self.model)

    def evaluate(self, dataset, mode=Task_Mode.Eval, epoch=None):
        data_loader = torch.utils.data.DataLoader(
            dataset,
            shuffle=False,
            batch_size=self.task_config.eval_batch_size,
            num_workers=0
        )
        metric = SeqEntityScore(self.slot_vocab.id2word, markup="bio")
        outputs = self.predict(self.model, data_loader)
        pred_intents = []
        golden_intents = []
        for output in outputs:
            intent_logits = output["intent_logits"]
            pre_intents = []
            intent_probs = torch.sigmoid(intent_logits)
            intent_ids = torch.gt(intent_probs, 0.8).nonzero().squeeze(1).numpy().tolist()
            for intent_id in intent_ids:
                intent = self.intent_vocab.id2word[intent_id]
                pre_intents.append(intent)
            pred_intents.append(pre_intents)

            slot_logits = output["slot_logits"]      
            text = output["utterance"]
            tag = slot_logits[1:-1]
            text_len = min(len(text), self.max_len - 2)
            assert len(tag) == text_len
            pred_tags = [self.slot_vocab.id2word[t] for t in tag]
            if mode == Task_Mode.Eval:
                true_intents = output['intents'].split(" ")
                golden_intents.append(true_intents)

                slot_ids = output['slot_ids'].cpu().numpy().tolist()
                label = slot_ids[1:text_len + 1]
                assert len(label) == text_len
                true_tags = [self.slot_vocab.id2word[l] for l in label]
                metric.update(pred_tags=[pred_tags], label_tags=[true_tags])
            else:
                entities = metric.get_entity(pred_tags=pred_tags)
                output["result"] = entities
    
        if mode == Task_Mode.Eval:
            precision, recall, f1 = calculateF1(golden_intents, pred_intents)
            eval_info, entity_info = metric.eval_result()
            logger.info(f"******* Evaluate: epoch={epoch}, step={self.global_step} *******")
            slot_info = ", ".join([f' {key} = {value:.4f} ' for key, value in eval_info.items()])
            logger.info(f"intent: p = {precision:.4f}, r = {recall:.4f}, f1 = {f1:.4f}")
            logger.info(f"slot: {slot_info}")
            return eval_info["f1"]
        else:
            return outputs

    def train(self, dataset, valid_dataset=None):
        logging.info(f"train dataset size = {len(dataset)}")
        if valid_dataset is not None:
            logging.info(f"valid dataset size = {len(valid_dataset)}")
        data_loader = torch.utils.data.DataLoader(
            dataset,
            shuffle=True,
            batch_size=self.task_config.train_batch_size,
            num_workers=0
        )
        num_train_steps = int(len(dataset) / self.task_config.train_batch_size * self.task_config.epochs)
        optimizer, scheduler = self.create_optimizer(self.model, use_scheduler=True, num_warmup_steps=1000,
                                                     num_train_steps=num_train_steps)
        self.model.train()
        # Train the model on each batch
        # Reset gradients
        loss_buffer = 0
        intent_losses= []
        slot_losses= []
        for epoch in range(self.task_config.epochs):
            for bi, batch in enumerate(data_loader):
                batch["intent_weights"] = self.intent_weights
                self.model.zero_grad()
                outputs = self.run_one_step(batch, self.model)
                # logits = outputs.pop("logits")
                intent_loss = outputs.pop("intent_loss")
                slot_loss = outputs.pop("slot_loss")
                intent_losses.append(intent_loss.item())
                slot_losses.append(slot_loss.item())
                loss = outputs.pop("loss")
                # Calculate gradients based on loss
                loss = loss.mean()
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()   #更新模型参数
                scheduler.step()  # 更新learning rate
                self.global_step += 1

                loss_buffer += loss.item()
                if self.global_step % self.task_config.nlog == 0:
                    logger.info("epoch={}, step={}, loss={:.4f}".format(epoch+1, self.global_step, loss_buffer / self.task_config.nlog))
                    loss_buffer = 0

            if valid_dataset != None:
                eval_score = self.evaluate(valid_dataset, mode=Task_Mode.Eval, epoch=epoch+1)
                self.model.train()
                if self.task_config.early_stop:
                    self.es(epoch, eval_score, self.model, model_path=self.task_config.model_save_path)
                    if self.es.early_stop:
                        logger.info("********** Early stopping ********")
                        break
            # 保存训练过程中的模型，防止意外程序停止，可以接着继续训练
            # self.save_checkpoint(model=self.model, model_path=self.task_config.model_save_path, epoch=epoch)
        logging.info(f"global step = {self.global_step}")
        intent_losses = np.array(intent_losses)
        slot_losses = np.array(slot_losses)
        np.save("/workspace/crosswoz_nlu/output/intent_losses.npy", intent_losses)
        np.save("/workspace/crosswoz_nlu/output/slot_losses.npy", slot_losses)
        self.plot_loss(self.global_step)

    def plot_loss(self, global_step):
        intent_losses = np.load("/workspace/crosswoz_nlu/output/intent_losses.npy")
        slot_losses = np.load("/workspace/crosswoz_nlu/output/slot_losses.npy")
        steps = np.arange(1, global_step + 1)
        plt.plot(steps, intent_losses)
        plt.plot(steps, slot_losses)
        plt.xlabel('training step')
        plt.ylabel('loss')
        plt.show()
        plt.savefig("/workspace/crosswoz_nlu/output/loss.jpg")
        plt.close()

    
    def read_data(self, file, mode):
        """
        根据不同任务编写数据处理，建议将原始数据进行预处理之后再在这里写数据处理成模型输入结构
        """
        dataset = []
        with open(file, "r", encoding="utf-8") as fin:
            data = json.load(fin)
            tk0 = tqdm(data.items(), total=len(data))
            
            for sess_id, sess in tk0:  
                for uttr in sess:
                    uttr_id = uttr["uttr_id"]
                    role = uttr["role"]
                    utterance = uttr["utterance"]
                    intents = uttr["intents"]
                    tags = uttr["tags"]
                    action = uttr["action"]

                    input_ids = [self.vocab.get_id("[CLS]")] + [self.vocab.word2id.get(t, self.vocab.get_id("[UNK]")) for t in utterance][:self.max_len - 2] + [self.vocab.get_id("[SEP]")]
                    token_type_ids = [0] * len(input_ids) + [0] * (self.max_len - len(input_ids))
                    input_masks = [1] * len(input_ids) + [0] * (self.max_len - len(input_ids))
                    # intent ids
                    intent_ids = np.zeros((self.intent_vocab.vocab_size), dtype=np.int)
                    for intent in intents:
                        idx = self.intent_vocab.word2id[intent]
                        self.intent_weights[idx] += 1
                        intent_ids[idx] = 1
                    # 槽位id
                    slot_ids = [0] + [self.slot_vocab.word2id.get(each, self.vocab.get_id("[UNK]")) for each in tags.split(" ")][:self.max_len - 2] + [0]
                    assert len(input_ids) == len(slot_ids)
                    
                    slot_ids = slot_ids + [0] * (self.max_len - len(slot_ids))
                    input_ids = input_ids + [0] * (self.max_len - len(input_ids))
                  
                    assert len(input_ids) == self.max_len
                    assert len(input_masks) == self.max_len
                    assert len(token_type_ids) == self.max_len
                    assert len(slot_ids) == self.max_len

                    dataset.append({
                        "sess_id": sess_id,
                        "uttr_id": uttr_id,
                        "intents": " ".join(intents),
                        "utterance": utterance,
                        'role': role,
                        'input_ids': torch.tensor(input_ids, dtype=torch.long),
                        'input_masks': torch.tensor(input_masks, dtype=torch.long),
                        'token_type_ids': torch.tensor(token_type_ids, dtype=torch.long),
                        'intent_ids': torch.tensor(intent_ids, dtype=torch.long),
                        'slot_ids': torch.tensor(slot_ids, dtype=torch.long),  
                    })
        keys = [k for k in range(self.intent_vocab.vocab_size)]
        intent_names = [self.intent_vocab.id2word[k] for k in keys]
        plt.bar(keys, self.intent_weights, align='center', alpha=0.7)
        plt.xticks(keys)
        plt.xlabel('class')
        plt.ylabel('number')
        plt.show()
        plt.savefig("/workspace/crosswoz_nlu/output/class_distribute.jpg")
        plt.close()

        # if mode == Task_Mode.Train:
        #     train_size = len(dataset)
        #     for intent, intent_id in self.intent_vocab.word2id.items():  
        #         neg_pos = (train_size - self.intent_weights[intent_id]) / self.intent_weights[intent_id]
        #         if neg_pos > 2:
        #             self.intent_weights[intent_id] = np.log10(neg_pos)  
        #         else:
        #             self.intent_weights[intent_id] = neg_pos   
        #     self.intent_weights = torch.tensor(self.intent_weights)

        return dataset


def seed_set(seed):
    '''
    set random seed of cpu and gpu
    '''
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

def run():
    config = Config()
    check_dir([config.model_save_path, config.output_path])
    seed_set(config.seed)
    os.environ["CUDA_VISIBLE_DEVICES"] = config.gpuids  # 设置gpu序号
    task_cls = find_task(config.task_name)
    task = task_cls(task_config=config)
    if config.do_train:
        dataset = task.read_data(config.train_data_path, mode=Task_Mode.Train)
        if config.do_eval:
            valid_dataset = task.read_data(config.dev_data_path[0], mode=Task_Mode.Eval)
            task.train(dataset, valid_dataset=valid_dataset)
        else:
            task.train(dataset)
    if config.do_eval:
        task.load_model(config.model_save_path)
        for dev_path in config.dev_data_path:
            logging.info(f"Evaluating model in {dev_path}")
            dataset = task.read_data(dev_path, mode=Task_Mode.Eval)
            logging.info(f"eval dataset size = {len(dataset)}")
            task.evaluate(dataset, mode=Task_Mode.Eval)
    if config.do_infer:
        task.load_model(config.model_save_path)
        for test_path in config.test_data_path:
            dataset = task.read_data(config.test_data_path, mode=Task_Mode.Infer)
            task.evaluate(dataset, mode=Task_Mode.Infer)

if __name__ == '__main__':
    run()
    
    
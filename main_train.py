import os
import pandas as pd 
import torch
import sklearn
import numpy as np
from sklearn.model_selection import train_test_split
from transformers import (
  AutoTokenizer,
  AutoConfig,
  AutoModelForSequenceClassification,
  Trainer,
  TrainingArguments,
  EarlyStoppingCallback,
  get_scheduler
  )
from transformers.utils import logging
import wandb
import argparse
from utilities.main_utilities import *
from utilities.criterion.loss import *
from dataloader.main_dataloader import *
from dataset.main_dataset import *
from preprocess.main_preprocess import *
from constants import *
from augmentation.main_augmentation import *

class CustomTrainer(Trainer):
    def __init__(self, loss_name, scheduler,num_training_steps, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loss_name= loss_name
        self.scheduler = scheduler
        self.num_training_steps = num_training_steps
    def compute_loss(self, model, inputs, return_outputs=False):
        labels = inputs.get("labels")
        # forward pass
        outputs = model(**inputs)
        logits = outputs.get("logits")
        # compute custom loss (suppose one has 3 labels with different weights)
        if self.loss_name == 'CE':
          loss_fct = nn.CrossEntropyLoss
        elif self.loss_name == 'LB':
          loss_fct = LabelSmoothingLoss()
        elif self.loss_name == 'focal':
          loss_fct = FocalLoss()
        elif self.loss_name == 'f1':
          loss_fct = F1Loss()
          
        loss = loss_fct(logits.view(-1, self.model.config.num_labels), labels.view(-1))
        return (loss, outputs) if return_outputs else loss

    def create_scheduler(self, num_training_steps, optimizer: torch.optim.Optimizer = None):
      if self.scheduler == 'linear' or self.scheduler == 'cosine':
        if self.scheduler == 'linear':
          my_scheduler = "linear"
        elif self.scheduler == 'cosine':
          my_scheduler = "cosine_with_restarts"

          self.lr_scheduler = get_scheduler(
              my_scheduler,
              optimizer=self.optimizer if optimizer is None else optimizer,
              num_warmup_steps=self.args.get_warmup_steps(num_training_steps),
              num_training_steps=num_training_steps,
          )

      elif self.scheduler == 'steplr':
        self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, step_size=1080, gamma=0.5)

      return self.lr_scheduler
    
def train(args):
    # load model and tokenizer
    MODEL_NAME = args.model
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    add_token = args.add_token

    # load dataset
    train_dataset = load_data(TRAIN_DIR, args.generate_option)

    train_label = label_to_num(train_dataset['label'].values)

    # tokenizing dataset
    tokenized_train = tokenized_dataset(train_dataset, tokenizer)

    if args.augmentation:
      tokenized_train = main_augmentation(tokenized_train)

    # make dataset for pytorch.
    RE_train_dataset = RE_Dataset(tokenized_train, train_label)
    X_train, X_val = train_test_split(RE_train_dataset, test_size=args.split_ratio, random_state=args.seed)

    # RE_dev_dataset = RE_Dataset(tokenized_dev, dev_label)
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    print("Training Start")
    print("="*100)
    print(f"DEVICE : {device}")
    # setting model hyperparameter
    model_config =  AutoConfig.from_pretrained(MODEL_NAME)
    model_config.num_labels = 30

    model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME, config=model_config)
    model.resize_token_embeddings(tokenizer.vocab_size + add_token)
    model.to(device)

    # 사용한 option 외에도 다양한 option들이 있습니다.
    # https://huggingface.co/transformers/main_classes/trainer.html#trainingarguments 참고해주세요.
    training_args = TrainingArguments(
        output_dir=SAVE_DIR,          # output directory
        save_total_limit=5,              # number of total save model.
        save_steps=len(train_dataset) // args.batch // 2, # model saving step.
        num_train_epochs=args.epochs,              # total number of training epochs
        learning_rate=args.lr,               # learning_rate
        per_device_train_batch_size=args.batch,  # batch size per device during training
        per_device_eval_batch_size=args.batch_valid,   # batch size for evaluation
        warmup_steps=args.warmup_steps,                # number of warmup steps for learning rate scheduler
        weight_decay=args.warmup,               # strength of weight decay
        logging_dir=LOG_DIR,            # directory for storing logs
        logging_steps=args.logging_steps,              # log saving step.
        evaluation_strategy='steps', # evaluation strategy to adopt during training
                                    # `no`: No evaluation during training.
                                    # `steps`: Evaluate every `eval_steps`.
                                    # `epoch`: Evaluate every end of epoch.
        eval_steps = len(train_dataset) //  args.batch // 2, # evaluation step.
        load_best_model_at_end = True,
        metric_for_best_model = args.metric_for_best_model,
        report_to='wandb',
    )

    trainer = CustomTrainer(
        model=model,                         # the instantiated 🤗 Transformers model to be trained
        args=training_args,                  # training arguments, defined above
        train_dataset=X_train,         # training dataset
        eval_dataset=X_val,             # evaluation dataset
        compute_metrics=compute_metrics,         # define metrics function
        callbacks = [EarlyStoppingCallback(early_stopping_patience=2)],
        loss_name = args.loss,
        scheduler = args.scheduler,
        num_training_steps = args.epochs * len(train_dataset) //  args.batch
        # num_training_steps = args.epochs * len(train_dataset) //  args.batch // 3
    )

    # train model
    trainer.train()
    path = os.path.join(BEST_MODEL_DIR, args.wandb_name)
    model.save_pretrained(path)

def main():
    parser = argparse.ArgumentParser()

    """path, model option"""
    parser.add_argument('--seed', type=int, default=42,
                        help='random seed (default: 42)')
    parser.add_argument('--wandb_path', type= str, default= 'test-project',
                        help='wandb graph, save_dir basic path (default: test-project') 
    parser.add_argument('--model', type=str, default='klue/roberta-large',
                        help='model type (default: klue/roberta-large)')
    parser.add_argument('--loss', type=str, default= 'focal',
                        help='LB: LabelSmoothing, CE: CrossEntropy, focal: Focal, f1:F1loss')
    parser.add_argument('--scheduler', type=str, default= 'linear',
                        help='linear, cosine, steplr')
    parser.add_argument('--wandb_name', type=str, default= 'test',
                        help='wandb name (default: test)')

    """hyperparameter"""
    parser.add_argument('--epochs', type=int, default=20,
                        help='number of epochs to train (default: 20)')
    parser.add_argument('--lr', type=float, default=5e-5,
                        help='learning rate (default: 5e-5)')
    parser.add_argument('--batch', type=int, default=32,
                        help='input batch size for training (default: 32)')
    parser.add_argument('--batch_valid', type=int, default=32,
                        help='input batch size for validing (default: 32)')
    parser.add_argument('--warmup', type=float, default=0.1,
                        help='warmup_ratio (default: 0.1)')
    parser.add_argument('--logging_steps', type=int,
                        default=100, help='logging_steps (default: 100)')
    parser.add_argument('--weight_decay', type=float,
                        default=0.01, help='weight_decay (default: 0.01)')
    parser.add_argument('--metric_for_best_model', type=str, default='f1',
                        help='metric_for_best_model (default: f1)')
    parser.add_argument('--add_token', type=int, default=15,
                        help='add token count (default: 15)')
    parser.add_argument('--split_ratio', type=float, default=0.2,
                        help='Test Val split ratio (default : 0.2)')
    parser.add_argument('--augmentation', type=bool, default=True,
                        help='Apply Random Masking/Delteing (default=False)')
    parser.add_argument('--generate_option', type=int, default=0,
                        help='0 : original / 1 : generated / 2 : concat')
    parser.add_argument('--warmup_steps', type=int,default= 810,
                        help='warmup_steps (default: 810)')

    args= parser.parse_args()
    wandb.init(name=args.wandb_name, project=args.wandb_path, entity=WANDB_ENT, config = vars(args),)

    logging.set_verbosity_warning()
    logger = logging.get_logger()
    logger.warning("\n")
    
    train(args)

if __name__ == '__main__':
    main()
from __future__ import absolute_import, division, print_function

import argparse
import glob
import json
import logging
import os
import random
import math

import numpy as np
import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tensorboardX import SummaryWriter
from tqdm import tqdm, trange
from scipy.special import softmax
import pathlib

from pytorch_transformers import (WEIGHTS_NAME, BertConfig,
                                  BertForSequenceClassification, BertForMultipleChoice,
                                  BertTokenizer,
                                  RobertaConfig, RobertaForSequenceClassification, RobertaTokenizer,
                                  XLMConfig, XLMForSequenceClassification,
                                  XLMTokenizer, XLNetConfig,
                                  XLNetForSequenceClassification,
                                  XLNetTokenizer)

from pytorch_transformers import AdamW, WarmupLinearSchedule

from model import RobertaForRRWithNodeEdgeLoss, MyModel
from utils import (compute_metrics,
                   output_modes, processors,
                   convert_examples_to_features_RR)

logger = logging.getLogger(__name__)

ALL_MODELS = sum(
    (tuple(conf.pretrained_config_archive_map.keys()) for conf in (BertConfig, XLNetConfig, XLMConfig, RobertaConfig)),
    ())

MODEL_CLASSES = {
    'roberta_rr': (RobertaConfig, RobertaForRRWithNodeEdgeLoss, RobertaTokenizer),
    'roberta_probr': (RobertaConfig, MyModel, RobertaTokenizer),
}

def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

def dp_or_ddp_model(args, model):
    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)
    return model


def train(args, train_dataset, model, tokenizer):
    """ Train the model """
    set_seed(args)  # Added here for reproductibility (even between python 2 and 3)
    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter()

    processor = processors[args.task_name]()

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    if args.warmup_pct is None:
        scheduler = WarmupLinearSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=t_total)
    else:
        scheduler = WarmupLinearSchedule(optimizer, warmup_steps=math.floor(args.warmup_pct * t_total), t_total=t_total)

    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    model = dp_or_ddp_model(args, model)

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
                args.train_batch_size * args.gradient_accumulation_steps * (
                    torch.distributed.get_world_size() if args.local_rank != -1 else 1))
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    tr_loss, logging_loss = 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(int(args.num_train_epochs), desc="Epoch", disable=args.local_rank not in [-1, 0])
    # set_seed(args)  # Added here for reproductibility (even between python 2 and 3)
    best_accu = 0.0
    best_epoch = 0
    for epoch_index, _ in enumerate(train_iterator):
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0],
                              mininterval=10, ncols=100)
        for step, batch in enumerate(epoch_iterator):
            model.train()
            batch = tuple(t.to(args.device) for t in batch)
            inputs = {'input_ids': batch[0],
                      'attention_mask': batch[1],
                      'token_type_ids': batch[2] if args.model_type in ['bert', 'xlnet', 'bert_mc'] else None,
                      # XLM don't use segment_ids
                      'proof_offset': batch[3],
                      'node_label': batch[4],
                      'edge_label': batch[5],
                      'labels': batch[6]}
            outputs = model(**inputs)
            loss, qa_loss, node_loss, edge_loss = outputs[:4]  # model outputs are always tuple in pytorch-transformers (see doc)

            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel training
            logger.info("Loss = %.4f, QA Loss = %.4f, Node Loss = %.4f, Edge Loss = %.4f" %
                        (loss, qa_loss.mean(), node_loss.mean(), edge_loss.mean()))
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
                torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
            else:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

            tr_loss += loss.item()
            if (step + 1) % args.gradient_accumulation_steps == 0:
                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1
            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break
        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break
        if (epoch_index + 1) % 5 == 0:
            output_dir = os.path.join(args.output_dir, "epoch-{}".format(epoch_index+1))
            if not os.path.exists(output_dir) and args.local_rank in [-1, 0]:
                os.makedirs(output_dir)
            logger.info("Saving model checkpoint to {} in epoch {}".format(output_dir, epoch_index+1))
            model_to_save = model.module if hasattr(model,
                                                    'module') else model  # Take care of distributed/parallel training
            model_to_save.save_pretrained(output_dir)
            tokenizer.save_pretrained(output_dir)
            model_to_save.save_pretrained(args.output_dir)
            tokenizer.save_pretrained(args.output_dir)

        #  evaluate(args, model, tokenizer, processor, prefix=global_step, eval_split="dev")
        #  accu = get_metric_on_dev(args)
        #  if accu > best_accu and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
            #  # Create output directory if needed
            #  if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
                #  os.makedirs(args.output_dir)
            #  logger.info("Saving model checkpoint to {} in epoch {}".format(args.output_dir, epoch_index))
            #  # Save a trained model, configuration and tokenizer using `save_pretrained()`.
            #  # They can then be reloaded using `from_pretrained()`
            #  model_to_save = model.module if hasattr(model,
                                                    #  'module') else model  # Take care of distributed/parallel training
            #  model_to_save.save_pretrained(args.output_dir)
            #  tokenizer.save_pretrained(args.output_dir)
            #  os.system("cp %s/dev_eval.log %s/best_dev_eval.log" % (args.output_dir, args.output_dir))

            #  if args.patience != -1 and (epoch_index - best_epoch) > args.patience:
                #  break
            #  best_accu = accu
            #  best_epoch = epoch_index

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step

def get_metric_on_dev(args):
    cmd = "python ./ilp_infer/inference.py "
    cmd += "--data_dir %s " % args.data_dir
    cmd += "--eval_split dev "
    cmd += "--node_preds %s/prediction_nodes_dev.lst " % args.output_dir
    cmd += "--edge_logits  %s/prediction_edge_logits_dev.lst " % args.output_dir
    cmd += "--edge_preds %s/edge_preds_d5_dev.lst " % args.output_dir
    cmd += "> %s/dev_inference.log " % args.output_dir
    os.system(cmd)
    cmd = "python ./evaluation/eval_proof.py "
    cmd += "--data_dir %s " % args.data_dir
    cmd += "--eval_split dev "
    cmd += "--qa_pred_file %s/predictions_dev.lst " % args.output_dir
    cmd += "--node_pred_file %s/prediction_nodes_dev.lst " % args.output_dir
    cmd += "--edge_pred_file %s/edge_preds_d5_dev.lst " % args.output_dir
    cmd += "> %s/dev_eval.log" % args.output_dir
    os.system(cmd)

    lines = []
    with open("%s/dev_eval.log" % args.output_dir, "r", encoding="utf8") as f:
        for line in f:
            lines.append(line.strip())
    return float(lines[-1].split('=')[-1])

def evaluate(args, model, tokenizer, processor, prefix="", eval_split=None):
    eval_task_names = (args.task_name,)
    eval_outputs_dirs = (args.output_dir,)

    assert eval_split is not None

    results = {}
    if os.path.exists("/output/metrics.json"):
        with open("/output/metrics.json", "r") as f:
            existing_results = json.loads(f.read())
        f.close()
        results.update(existing_results)

    for eval_task, eval_output_dir in zip(eval_task_names, eval_outputs_dirs):
        eval_dataset, examples = load_and_cache_examples(args, eval_task, tokenizer, evaluate=True,
                                                         eval_split=eval_split)

        if not os.path.exists(eval_output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(eval_output_dir)

        args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
        # Note that DistributedSampler samples randomly
        eval_sampler = SequentialSampler(eval_dataset) if args.local_rank == -1 else DistributedSampler(eval_dataset)
        eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

        # Eval!
        logger.info("***** Running evaluation {} on {} *****".format(prefix, eval_split))
        logger.info("  Num examples = %d", len(eval_dataset))
        logger.info("  Batch size = %d", args.eval_batch_size)
        eval_loss = 0.0
        nb_eval_steps = 0
        preds = None
        node_preds = None
        edge_preds = None
        out_label_ids = None
        out_node_label_ids = None
        out_edge_label_ids = None
        for batch in tqdm(eval_dataloader, desc="Evaluating", mininterval=10, ncols=100):
            model.eval()
            batch = tuple(t.to(args.device) for t in batch)

            with torch.no_grad():
                inputs = {'input_ids': batch[0],
                          'attention_mask': batch[1],
                          'token_type_ids': batch[2] if args.model_type in ['bert', 'xlnet', 'bert_mc'] else None,
                          # XLM don't use segment_ids
                          'proof_offset': batch[3],
                          'node_label': batch[4],
                          'edge_label': batch[5],
                          'labels': batch[6]}
                outputs = model(**inputs)
                tmp_eval_loss, tmp_qa_loss, tmp_node_loss, tmp_edge_loss, logits, node_logits, edge_logits = outputs[:7]
                logger.info("Loss = %.4f, QA Loss = %.4f, Node Loss = %.4f, Edge Loss = %.4f" %
                            (tmp_eval_loss.mean(), tmp_qa_loss.mean(), tmp_node_loss.mean(), tmp_edge_loss.mean()))

                eval_loss += tmp_eval_loss.mean().item()
            nb_eval_steps += 1
            if preds is None:
                preds = logits.detach().cpu().numpy()
                node_preds = node_logits.detach().cpu().numpy()
                edge_preds = edge_logits.detach().cpu().numpy()
                out_label_ids = inputs['labels'].detach().cpu().numpy()
                out_node_label_ids = inputs['node_label'].detach().cpu().numpy()
                out_edge_label_ids = inputs['edge_label'].detach().cpu().numpy()
            else:
                preds = np.append(preds, logits.detach().cpu().numpy(), axis=0)
                node_preds = np.append(node_preds, node_logits.detach().cpu().numpy(), axis=0)
                edge_preds = np.append(edge_preds, edge_logits.detach().cpu().numpy(), axis=0)
                out_label_ids = np.append(out_label_ids, inputs['labels'].detach().cpu().numpy(), axis=0)
                out_node_label_ids = np.append(out_node_label_ids,
                                                    inputs['node_label'].detach().cpu().numpy(), axis=0)
                out_edge_label_ids = np.append(out_edge_label_ids,
                                                inputs['edge_label'].detach().cpu().numpy(), axis=0)

        eval_loss = eval_loss / nb_eval_steps
        preds = np.argmax(preds, axis=1)
        node_preds = np.argmax(node_preds, axis=2)

        normalized_logits = softmax(edge_preds, axis=2)
        edge_pred_logits = normalized_logits[:, :, 1]
        edge_preds = np.argmax(edge_preds, axis=2)


        result = compute_metrics(eval_task, preds, out_label_ids)
        result_split = {}
        for k, v in result.items():
            result_split[k + "_{}".format(eval_split)] = v
        results.update(result_split)

        output_eval_file = os.path.join(eval_output_dir, "eval_results_{}.txt".format(eval_split))
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results {} on {} *****".format(prefix, eval_split))
            for key in sorted(result_split.keys()):
                logger.info("  %s = %s", key, str(result_split[key]))
                writer.write("%s = %s\n" % (key, str(result_split[key])))

        # The model outputs the QA accuracy, QA predictions, node predictions and the edge logit predictions

        # QA Predictions
        output_pred_file = os.path.join(eval_output_dir, "predictions_{}.lst".format(eval_split))
        with open(output_pred_file, "w") as writer:
            logger.info("***** Write predictions {} on {} *****".format(prefix, eval_split))
            for pred in preds:
                writer.write("{}\n".format(processor.get_labels()[pred]))

        # prediction nodes
        output_node_pred_file = os.path.join(eval_output_dir, "prediction_nodes_{}.lst".format(eval_split))
        with open(output_node_pred_file, "w") as writer:
            logger.info("***** Write predictions {} on {} *****".format(prefix, eval_split))
            for node_gold, node_pred in zip(out_node_label_ids, node_preds):
                node_gold = node_gold[np.where(node_gold != -100)[0]]
                node_pred = node_pred[:len(node_gold)]
                writer.write(str(list(node_pred)) + "\n")

        # prediction edge logits
        output_edge_pred_file = os.path.join(eval_output_dir, "prediction_edge_logits_{}.lst".format(eval_split))
        with open(output_edge_pred_file, "w") as writer:
            logger.info("***** Write predictions {} on {} *****".format(prefix, eval_split))
            for edge_pred_logit in edge_pred_logits:
                writer.write(str(list(edge_pred_logit)) + "\n")

    return results


def load_and_cache_examples(args, task, tokenizer, evaluate=False, eval_split="train"):
    processor = processors[task]()
    output_mode = output_modes[task]
    # Load data features from cache or dataset file
    if args.data_cache_dir is None:
        data_cache_dir = args.data_dir
    else:
        data_cache_dir = args.data_cache_dir

    cached_features_file = os.path.join(data_cache_dir, 'cached_{}_{}_{}_{}'.format(
        eval_split,
        list(filter(None, args.model_name_or_path.split('/'))).pop(),
        str(args.max_seq_length),
        str(task)))

    if os.path.exists(cached_features_file):
        logger.info("Loading features from cached file %s", cached_features_file)
        features = torch.load(cached_features_file)
        if eval_split == "dev":
            examples = processor.get_dev_examples(args.data_dir)
        else:
            examples = None
    else:
        logger.info("Creating features from dataset file at %s", args.data_dir)
        label_list = processor.get_labels()

        if eval_split == "train":
            examples = processor.get_train_examples(args.data_dir)
        elif eval_split == "dev":
            examples = processor.get_dev_examples(args.data_dir)
        elif eval_split == "test":
            examples = processor.get_test_examples(args.data_dir)
        else:
            raise Exception("eval_split should be among train / dev / test")

        features = convert_examples_to_features_RR(examples, label_list, args.max_seq_length, args.max_node_length, args.max_edge_length,
                                                   tokenizer, output_mode,
                                                   cls_token_at_end=bool(args.model_type in ['xlnet']),
                                                   # xlnet has a cls token at the end
                                                   cls_token=tokenizer.cls_token,
                                                   sep_token=tokenizer.sep_token,
                                                   sep_token_extra=bool(
                                                       args.model_type in ['roberta', "roberta_mc", "roberta_rr"]),
                                                   cls_token_segment_id=2 if args.model_type in ['xlnet'] else 0,
                                                   pad_on_left=bool(args.model_type in ['xlnet']),
                                                   # pad on the left for xlnet
                                                   pad_token=tokenizer.convert_tokens_to_ids([tokenizer.pad_token])[
                                                       0],
                                                   pad_token_segment_id=4 if args.model_type in ['xlnet'] else 0,
                                                   filter_mask=args.filter_mask)

    all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
    all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
    all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
    all_proof_offset = torch.tensor([f.proof_offset for f in features], dtype=torch.long)
    all_node_label = torch.tensor([f.node_label for f in features], dtype=torch.long)
    all_edge_label = torch.tensor([f.edge_label for f in features], dtype=torch.long)
    all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.long)

    dataset = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_proof_offset, all_node_label, all_edge_label,
                            all_label_ids)
    return dataset, examples


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir", default=None, type=str, required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--model_type", default=None, type=str, required=True,
                        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()))
    parser.add_argument("--model_name_or_path", default=None, type=str, required=True,
                        help="Path to pre-trained model or shortcut name selected in the list: " + ", ".join(
                            ALL_MODELS))
    parser.add_argument("--task_name", default=None, type=str, required=True,
                        help="The name of the task to train selected in the list: " + ", ".join(processors.keys()))
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    parser.add_argument("--data_cache_dir", default=None, type=str,
                        help="Cache dir if it needs to be diff from data_dir")

    ## Other parameters
    parser.add_argument("--config_name", default="", type=str,
                        help="Pretrained config name or path if not the same as model_name")
    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Pretrained tokenizer name or path if not the same as model_name")
    parser.add_argument("--cache_dir", default="", type=str,
                        help="Where do you want to store the pre-trained models downloaded from s3")
    parser.add_argument("--max_seq_length", default=300, type=int,
                        help="The maximum total input sequence length after tokenization. Sequences longer "
                             "than this will be truncated, sequences shorter will be padded.")
    parser.add_argument("--max_edge_length", default=676, type=int,
                        help="Maximum number of edges, chosen as (R+F+1)^2")
    parser.add_argument("--max_node_length", default=26, type=int,
                        help="Maximum number of nodes, chosen as (R+F+1)")
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_prediction", action='store_true',
                        help="Whether to run prediction on the test set. (Training will not be executed.)")
    parser.add_argument("--evaluate_during_training", action='store_true',
                        help="Rul evaluation during training at each logging step.")
    parser.add_argument("--do_lower_case", action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument('--run_on_test', action='store_true')
    parser.add_argument('--filter_mask', action='store_true')

    parser.add_argument("--per_gpu_train_batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--per_gpu_eval_batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=8,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--learning_rate", default=1e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.1, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-6, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--patience", default=-1, type=int,
                        help="Early stops the training if validation metric doesn't improve after a given patience.")
    parser.add_argument("--max_steps", default=-1, type=int,
                        help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--warmup_steps", default=0, type=int,
                        help="Linear warmup over warmup_steps.")
    parser.add_argument("--warmup_pct", default=None, type=float,
                        help="Linear warmup over warmup_pct*total_steps.")

    parser.add_argument('--logging_steps', type=int, default=50,
                        help="Log every X updates steps.")
    parser.add_argument('--save_steps', type=int, default=50,
                        help="Save checkpoint every X updates steps.")
    parser.add_argument("--eval_all_checkpoints", action='store_true',
                        help="Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number")
    parser.add_argument("--no_cuda", action='store_true',
                        help="Avoid using CUDA when available")
    parser.add_argument('--overwrite_output_dir', action='store_true',
                        help="Overwrite the content of the output directory")
    parser.add_argument('--overwrite_cache', action='store_true',
                        help="Overwrite the cached training and evaluation sets")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")

    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="For distributed training: local_rank")
    parser.add_argument('--server_ip', type=str, default='', help="For distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="For distant debugging.")
    args = parser.parse_args()

    if os.path.exists(args.output_dir) and os.listdir(
            args.output_dir) and args.do_train and not args.overwrite_output_dir:
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                args.output_dir))

    # Setup distant debugging if needed
    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd
        print("Waiting for debugger attach")
        ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
        ptvsd.wait_for_attach()

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend='nccl')
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
                   args.local_rank, device, args.n_gpu, bool(args.local_rank != -1), args.fp16)

    # Set seed
    set_seed(args)

    # Prepare GLUE task
    args.task_name = args.task_name.lower()
    if args.task_name not in processors:
        raise ValueError("Task not found: %s" % (args.task_name))
    processor = processors[args.task_name]()
    args.output_mode = output_modes[args.task_name]
    label_list = processor.get_labels()
    num_labels = len(label_list)

    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    args.model_type = args.model_type.lower()
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(
        args.config_name if args.config_name else args.model_name_or_path,
        num_labels=num_labels,
        finetuning_task=args.task_name
    )
    tokenizer = tokenizer_class.from_pretrained(args.tokenizer_name if args.tokenizer_name else args.model_name_or_path,
                                                do_lower_case=args.do_lower_case)
    model = model_class.from_pretrained(args.model_name_or_path, from_tf=bool('.ckpt' in args.model_name_or_path),
                                        config=config)

    if args.local_rank == 0:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    model.to(args.device)

    logger.info("Training/evaluation parameters %s", args)

    # Prediction (on test set)
    if args.do_prediction:
        results = {}
        logger.info("Prediction on the test set (note: Training will not be executed.) ")
        model = dp_or_ddp_model(args, model)
        result = evaluate(args, model, tokenizer, processor, prefix="", eval_split="test")
        result = dict((k, v) for k, v in result.items())
        results.update(result)
        logger.info("***** Experiment finished *****")
        return results

    # Training
    if args.do_train:
        train_dataset, _ = load_and_cache_examples(args, args.task_name, tokenizer, evaluate=False)
        global_step, tr_loss = train(args, train_dataset, model, tokenizer)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)

    # # Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
    # if args.do_train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
    #     # Create output directory if needed
    #     if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
    #         os.makedirs(args.output_dir)

    #     logger.info("Saving model checkpoint to %s", args.output_dir)
    #     # Save a trained model, configuration and tokenizer using `save_pretrained()`.
    #     # They can then be reloaded using `from_pretrained()`
    #     model_to_save = model.module if hasattr(model,
    #                                             'module') else model  # Take care of distributed/parallel training
    #     model_to_save.save_pretrained(args.output_dir)
    #     tokenizer.save_pretrained(args.output_dir)

    #     # Good practice: save your training arguments together with the trained model
    #     #  torch.save(args, os.path.join(args.output_dir, 'training_args.bin'))

    #     # Load a trained model and vocabulary that you have fine-tuned
    #     #  model = model_class.from_pretrained(args.output_dir)
    #     #  tokenizer = tokenizer_class.from_pretrained(args.output_dir)
    #     #  model.to(args.device)

    # Evaluation
    results = {}
    checkpoints = [args.output_dir]
    if args.do_eval and args.local_rank in [-1, 0]:
        if args.eval_all_checkpoints:
            checkpoints = list(
                os.path.dirname(c) for c in sorted(glob.glob(args.output_dir + '/**/' + WEIGHTS_NAME, recursive=True)))
            logging.getLogger("pytorch_transformers.modeling_utils").setLevel(logging.WARN)  # Reduce logging
        logger.info("Evaluate the following checkpoints: %s", checkpoints)
        for checkpoint in checkpoints:
            global_step = checkpoint.split('-')[-1] if len(checkpoints) > 1 else ""
            model = model_class.from_pretrained(checkpoint)
            model.to(args.device)
            model = dp_or_ddp_model(args, model)
            result = evaluate(args, model, tokenizer, processor, prefix=global_step, eval_split="dev")
            result = dict((k + '_{}'.format(global_step), v) for k, v in result.items())
            results.update(result)

    # Run on test
    if args.run_on_test and args.local_rank in [-1, 0]:
        checkpoint = checkpoints[0]
        global_step = checkpoint.split('-')[-1] if len(checkpoints) > 1 else ""
        model = model_class.from_pretrained(checkpoint)
        model.to(args.device)
        model = dp_or_ddp_model(args, model)
        result = evaluate(args, model, tokenizer, processor, prefix=global_step, eval_split="test")
        result = dict((k + '_{}'.format(global_step), v) for k, v in result.items())
        results.update(result)

    logger.info("***** Experiment finished *****")
    return results


if __name__ == "__main__":
    main()
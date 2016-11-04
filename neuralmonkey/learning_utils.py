# tests: mypy

import os
import codecs
import re
import numpy as np
import tensorflow as tf
from termcolor import colored

from neuralmonkey.logging import log, log_print

try:
    #pylint: disable=unused-import,bare-except,invalid-name,import-error,no-member
    from typing import Dict, List, Union, Tuple
    from neuralmonkey.decoders.decoder import Decoder
    Hypothesis = Tuple[float, List[int]]
    Feed_dict = Dict[tf.Tensor, np.Array]
except:
    pass

def load_tokenized(text_file, preprocess=None):
    """
    Loads a tokenized text file a list of list of tokens.

    Args:

        text_file: An opened file.

        preprocess: A function/callable that (linguistically) preprocesses the
            sentences

    """

    if not preprocess:
        preprocess = lambda x: x

    return [preprocess(re.split(r"[ ]", l.rstrip())) for l in text_file]




def get_eval_string(evaluators, evaluation_res):
    """ Formats the external evaluation metric for the console output. """
    eval_string = "    ".join(["{}: {:.2f}".format(f.name,
                                                   evaluation_res[f.name])
                               for f in evaluators[:-1]])

    if len(evaluators) >= 1:
        main_evaluator = evaluators[-1]

        eval_string += colored(
            "    {}: {:.2f}".format(main_evaluator.name,
                                    evaluation_res[main_evaluator.name]),
            attrs=['bold'])

    return eval_string


def training_loop(tf_manager,
                  epochs, trainer, batch_size,
                  train_dataset, val_dataset,
                  log_directory,
                  evaluators,
                  runners,
                  test_datasets=None,
                  save_n_best_vars=1,
                  link_best_vars="/tmp/variables.data.best",
                  vars_prefix="/tmp/variables.data",
                  logging_period=20,
                  validation_period=500,
                  postprocess=None,
                  minimize_metric=False):

    """
    Performs the training loop for given graph and data.

    Args:

        tf_manager: TensorFlowManager with initialized sessions.

        epochs: Number of epochs for which the algoritm will learn.

        trainer: The trainer object containg the TensorFlow code for computing
            the loss and optimization operation.

        train_dataset:

        val_dataset:

        postprocess: Function that takes the output sentence as produced by the
            decoder and transforms into tokenized sentence.

        log_directory: Directory where the TensordBoard log will be generated.
            If None, nothing will be done.

        evaluators: List of evaluators. The last evaluator
            is used as the main. Each function accepts list of decoded sequences
            and list of reference sequences and returns a float.

    """
    all_coders = encoders + [decoder]

    if not postprocess:
        postprocess = lambda x: x

    evaluation_labels = [f.name for f in evaluators]
    step = 0
    seen_instances = 0

    if save_n_best_vars < 1:
        raise Exception('save_n_best_vars must be greater than zero')

    if save_n_best_vars == 1:
        variables_files = [vars_prefix]
    elif save_n_best_vars > 1:
        variables_files = ['{}.{}'.format(vars_prefix, i)
                           for i in range(save_n_best_vars)]

    if minimize_metric:
        saved_scores = [np.inf for _ in range(save_n_best_vars)]
        best_score = np.inf
    else:
        saved_scores = [-np.inf for _ in range(save_n_best_vars)]
        best_score = -np.inf

    saver.save(sess, variables_files[0])

    if os.path.islink(link_best_vars):
        # if overwriting output dir
        os.unlink(link_best_vars)
    os.symlink(os.path.basename(variables_files[0]), link_best_vars)

    if log_directory:
        log("Initializing TensorBoard summary writer.")
        tb_writer = tf.train.SummaryWriter(log_directory, tf_manager.session[0].graph)
        log("TesorBoard writer initialized.")

    best_score_epoch = 0
    best_score_batch_no = 0

    # TODO collect all output series for all runners
    val_raw_tgt_sentences = val_dataset.get_series(decoder.data_id)
    val_tgt_sentences = postprocess(val_raw_tgt_sentences)

    log("Starting training")
    try:
        for i in range(epochs):
            log_print("")
            log("Epoch {} starts".format(i + 1), color='red')

            train_dataset.shuffle()
            train_batched_datasets = train_dataset.batch_dataset(batch_size)

            for batch_n, batch_dataset in enumerate(train_batched_datasets):

                step += 1
                batch_sentences = batch_dataset.get_series(decoder.data_id)
                seen_instances += len(batch_sentences)
                if step % logging_period == logging_period - 1:
                    summary_str = trainer.run(sess, batch_feed_dict, summary=True)
                    _, _, train_evaluation = \
                            run_on_dataset(tf_manager, runners, batch_dataset,
                                           evaluators, postprocess, write_out=False)

                    _log_evaluation(evaluators, tb_writer, train_evaluation,
                                    seen_instances, summary_str, None, train=True)
                else:
                    trainer.run(sess, batch_feed_dict, summary=False)

                if step % validation_period == validation_period - 1:
                    decoded_val_sentences, decoded_raw_val_sentences, \
                        val_evaluation = run_on_dataset(
                            tf_manager, runners, val_dataset,
                            evaluators, postprocess, write_out=False)

                    this_score = val_evaluation[evaluators[-1].name]

                    def is_better(score1, score2, minimize):
                        if minimize:
                            return score1 < score2
                        else:
                            return score1 > score2

                    def argworst(scores, minimize):
                        if minimize:
                            return np.argmax(scores)
                        else:
                            return np.argmin(scores)

                    if is_better(this_score, best_score, minimize_metric):
                        best_score = this_score
                        best_score_epoch = i + 1
                        best_score_batch_no = batch_n

                    worst_index = argworst(saved_scores, minimize_metric)
                    worst_score = saved_scores[worst_index]

                    if is_better(this_score, worst_score, minimize_metric):
                        # we need to save this score instead the worst score
                        worst_var_file = variables_files[worst_index]
                        tf_manager.save(worst_var_file)
                        saved_scores[worst_index] = this_score
                        log("Variable file saved in {}".format(worst_var_file))

                        # update symlink
                        if best_score == this_score:
                            os.unlink(link_best_vars)
                            os.symlink(os.path.basename(worst_var_file), link_best_vars)

                        log("Best scores saved so far: {}".format(saved_scores))

                    log("Validation (epoch {}, batch number {}):"
                        .format(i + 1, batch_n), color='blue')

                    _log_evaluation(evaluators, tb_writer,
                                    val_evaluation, seen_instances,
                                    summary_str, None, train=False)

                    if this_score == best_score:
                        best_score_str = colored("{:.2f}".format(best_score),
                                                 attrs=['bold'])
                    else:
                        best_score_str = "{:.2f}".format(best_score)

                    log("best {} on validation: {} (in epoch {}, "
                        "after batch number {})"
                        .format(evaluation_labels[-1], best_score_str,
                                best_score_epoch, best_score_batch_no),
                        color='blue')


                    log_print("")
                    log_print("Examples:")
                    for sent, sent_raw, ref_sent, ref_sent_raw, src_sents in zip(
                            decoded_val_sentences[:15],
                            decoded_raw_val_sentences,
                            val_tgt_sentences,
                            val_raw_tgt_sentences,
                            val_src_sentences_by_sentidx):


                        for src_sent in src_sents:
                            log_print(colored(
                                "      src: {}".format(" ".join(src_sent)),
                                color="grey"))


                        if isinstance(sent, list):
                            #log_print("      raw: {}"
                            #          .format(" ".join(sent_raw)))
                            log_print("      out: {}".format(" ".join(sent)))
                        else:
                            # TODO does this code ever execute?
                            #log_print(sent_raw)
                            log_print(sent)

                        #log_print(colored(
                        #    " raw ref.: {}".format(" ".join(ref_sent_raw)),
                        #    color="magenta"))
                        log_print(colored(
                            "      ref: {}".format(" ".join(ref_sent)),
                            color="magenta"))

                        log_print("")

                    tb_writer.add_summary(val_plots[0], step)

    except KeyboardInterrupt:
        log("Training interrupted by user.")

    if os.path.islink(link_best_vars):
        tf_manager.restore(link_best_vars)

    log("Training finished. Maximum {} on validation data: {:.2f}, epoch {}"
        .format(evaluation_labels[-1], best_score, best_score_epoch))

    for dataset in test_datasets:
        _, _, evaluation = run_on_dataset(tf_manager, runners,
                                          dataset, evaluators,
                                          postprocess, write_out=True)
        if evaluation:
            _print_dataset_evaluation(dataset.name, evaluation)

    log("Finished.")


def run_on_dataset(sessions, runner, all_coders, decoder, dataset,
                   evaluators, postprocess, write_out=False,
                   extra_fetches=None):
    """
    Applies the model on a dataset and optionally writes outpus into a file.

    Args:

        tf_manager: TensorFlow manager with initialized sessions.

        runners: A function that runs the code

        dataset: The dataset on which the model will be executed.

        evaluators: List of evaluators that are used for the model
            evaluation if the target data are provided.

        postprocess: an object to use as postprocessing of the

        write_out: Flag whether the outputs should be printed to a file defined
            in the dataset object.

        extra_fetches: Extra tensors to evaluate for each batch.

    Returns:

        Tuple of resulting sentences/numpy arrays, and evaluation results if
            they are available which are dictionary function -> value.

    """
    # TODO runner should know which series it produces and not decoder
    # TODO evaluators should be dict: series_name -> list of evaluators
    all_results = tf_manager.execute(dataset, runners, train=train, batch_size=batch_size)

    if postprocess is not None:
        result = postprocess(result_raw)
    else:
        result = result_raw

    if write_out:
        if decoder.data_id in dataset.series_outputs:
            path = dataset.series_outputs[decoder.data_id]
            if isinstance(result, np.ndarray):
                np.save(path, result)
                log("Result saved as numpy array to \"{}\"".format(path))
            else:
                with codecs.open(path, 'w', 'utf-8') as f_out:
                    f_out.writelines([" ".join(sent)+"\n" for sent in result])
                log("Result saved as plain text \"{}\"".format(path))
        else:
            log("There is no output file for dataset: {}"\
                    .format(dataset.name), color='red')

    evaluation = {}
    if dataset.has_series(decoder.data_id):
        test_targets = dataset.get_series(decoder.data_id)
        evaluation["opt_loss"] = opt_loss
        evaluation["dec_loss"] = dec_loss
        for func in evaluators:
            evaluation[func.name] = func(result, test_targets)

    if extra_fetches is not None:
        return result, result_raw, evaluation, evaluated_fetches
    else:
        return result, result_raw, evaluation

# pylint: disable=too-many-arguments
def _log_evaluation(evaluators, tb_writer, eval_result,
                    seen_instances,
                    summary_str, histograms_str, train=False):
    """Log the evaluation results and the TensorBoard summaries."""

    def format_eval_name(name):
        if hasattr(name, '__call__'):
            return name.__name__
        else:
            return str(name)

    if train:
        color = 'yellow'
        prefix = 'train'
    else:
        color = 'blue'
        prefix = 'val'

    eval_string = get_eval_string(evaluators, eval_result)

    log("opt. loss: {:.4f}    dec. loss: {:.4f}    {}"\
            .format(eval_result['opt_loss'],
                    eval_result['dec_loss'],
                    eval_string),
        color=color)

    if tb_writer:
        tb_writer.add_summary(summary_str, seen_instances)
        if histograms_str:
            tb_writer.add_summary(histograms_str, seen_instances)
        external_str = \
            tf.Summary(value=[tf.Summary.Value(tag=prefix+"_"+format_eval_name(name),
                                               simple_value=value)\
                              for name, value in eval_result.items()])

        tb_writer.add_summary(external_str, seen_instances)


def _print_dataset_evaluation(name, evaluation):
    line_len = 22
    log("Evaluating model on \"{}\"".format(name))

    log("... optimization loss:      {:.4f}".format(evaluation['opt_loss']))
    log("... runtime loss:           {:.4f}".format(evaluation['opt_loss']))

    for func in evaluation:
        if hasattr(func, '__call__'):
            name = func.__name__
            space = "".join([" " for _ in range(line_len - len(name))])
            log("... {}:{} {:.4f}".format(name, space, evaluation[func]))

    log_print("")

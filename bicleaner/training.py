from multiprocessing import Queue, Process, Value, cpu_count
from heapq import heappush, heappop
from tempfile import TemporaryFile, NamedTemporaryFile
from fuzzywuzzy import process, fuzz
import logging
import os
import random
import math
import typing
import fasttext

try:
    from .tokenizer import Tokenizer
except (SystemError, ImportError):
    from tokenizer import Tokenizer

# Porn removal classifier
# training, compressing, run tests and save model file
def train_porn_removal(args):
    if args.porn_removal_train is None or args.porn_removal_file is None:
        return

    logging.info("Training porn removal classifier.")
    model = fasttext.train_supervised(args.porn_removal_train.name,
                                    thread=args.processes,
                                    lr=1.0,
                                    epoch=25,
                                    minCount=5,
                                    wordNgrams=1,
                                    verbose=0)
    logging.info("Compressing classifier.")
    model.quantize(args.porn_removal_train.name,
                retrain=True,
                thread=args.processes,
                verbose=0)

    if args.porn_removal_test is not None:
        N, p, r = model.test(args.porn_removal_test.name, threshold=0.5)
        logging.info("Precision:\t{:.3f}".format(p))
        logging.info("Recall:\t{:.3f}".format(r))

    logging.info("Saving porn removal classifier.")
    model.save_model(args.porn_removal_file)

# Generate negative and positive samples for a sentence pair
def sentence_noise(i, src, trg, args):
    size = len(src)
    sts = []
    src_strip = src[i].strip()
    trg_strip = trg[i].strip()

    # Positive samples
    for j in range(args.pos_ratio):
        sts.append(src_strip + "\t" + trg_strip+ "\t1")

    # Random misalignment
    for j in range(args.rand_ratio):
        sts.append(src[random.randrange(1,size)].strip() + "\t" + trg_strip + "\t0")

    # Frequence based noise
    tokenizer = Tokenizer(args.target_tokenizer_command, args.target_lang)
    for j in range(args.freq_ratio):
        t_toks = tokenizer.tokenize(trg[i])
        replaced = add_freqency_replacement_noise_to_sentence(t_toks, args.tl_word_freqs)
        if replaced is not None:
            sts.append(src_strip + "\t" + tokenizer.detokenize(replaced) + "\t0")

    # Randomly omit words
    tokenizer = Tokenizer(args.target_tokenizer_command, args.target_lang)
    for j in range(args.womit_ratio):
        t_toks = tokenizer.tokenize(trg[i])
        omitted = remove_words_randomly_from_sentence(t_toks)
        sts.append(src_strip + "\t" + tokenizer.detokenize(omitted) + "\t0")

    # Misalginment by fuzzy matching
    if args.fuzzy_ratio > 0:
        explored = {n:trg[n] for n in random.sample(range(size), min(3000, size))}
        matches = process.extract(trg[i], explored,
                                  scorer=fuzz.token_sort_ratio,
                                  limit=25)
        m_index = [m[2] for m in matches if m[1]<70][:args.fuzzy_ratio]
        for m in m_index:
            sts.append(src_strip + "\t" + trg[m].strip() + "\t0")

    # Misalgniment with neighbour sentences
    if args.neighbour_mix and i <size-2 and i > 1:
        sts.append(src_strip + "\t" + trg[i+1].strip()+ "\t0")
        sts.append(src_strip + "\t" + trg[i-1].strip()+ "\t0")

    return sts

# Take block number from the queue and generate noise for that block
def worker_process(num, src, trg, jobs_queue, output_queue, args):
    nlines = len(src)

    while True:
        job = jobs_queue.get()

        if job is not None:
            logging.debug("Job {0}".format(job.__repr__()))

            # Generate noise for each sentence in the block
            output = []
            for i in range(job, min(job+args.block_size, nlines)):
                output.extend(sentence_noise(i, src, trg, args))

            output_file = NamedTemporaryFile('w+', delete=False)
            for j in output:
                output_file.write(j + '\n')
            output_file.close()
            output_queue.put((job,output_file.name))
        else:
            logging.debug(f"Exiting worker {num}")
            break

# Merges all the temporary files from the workers
def reduce_process(output_queue, output_file, block_size):
    h = []
    last_block = 0
    while True:
        logging.debug("Reduce: heap status {0}".format(h.__str__()))
        while len(h) > 0 and h[0][0] == last_block:
            nblock, filein_name = heappop(h)
            last_block += block_size

            with open(filein_name, 'r') as filein:
                for i in filein:
                    output_file.write(i)
            os.unlink(filein_name)

        job = output_queue.get()
        if job is not None:
            nblock, filein_name = job
            heappush(h, (nblock, filein_name))
        else:
            logging.debug("Exiting reduce loop")
            break

    if len(h) > 0:
        logging.debug(f"Still elements in heap: {h}")

    while len(h) > 0 and h[0][0] == last_block:
        nblock, filein_name = heappop(h)
        last_block += block_size

        with open(filein_name, 'r') as filein:
            for i in filein:
                output_file.write(i)

        os.unlink(filein_name)

    if len(h) != 0:
        logging.error("The queue is not empty and it should!")

    output_file.close()


# Parallel loop over input sentences to generate noise
def build_noise(input, args):
    src = []
    trg = {}
    # Read sentences into memory
    for i, line in enumerate(input):
        parts = line.rstrip("\n").split("\t")
        src.append(parts[0])
        trg[i] = parts[1]
    size = len(src)

    logging.debug("Running {0} workers at {1} rows per block".format(args.processes, args.block_size))
    process_count = max(1, args.processes)
    maxsize = 1000 * process_count
    output_queue = Queue(maxsize = maxsize)
    worker_count = process_count
    output_file = NamedTemporaryFile('w+', delete=False)

    # Start reducer
    reduce = Process(target = reduce_process,
                     args   = (output_queue, output_file, args.block_size))
    reduce.start()

    # Start workers
    jobs_queue = Queue(maxsize = maxsize)
    workers = []
    for i in range(worker_count):
        worker = Process(target = worker_process,
                         args   = (i, src, trg, jobs_queue, output_queue, args))
        worker.daemon = True # dies with the parent process
        worker.start()
        workers.append(worker)

    # Map jobs
    for i in range(0, size, args.block_size):
        jobs_queue.put(i)

    # Worker termination
    for _ in workers:
        jobs_queue.put(None)

    for w in workers:
        w.join()

    # Reducer termination
    output_queue.put(None)
    reduce.join()

    return output_file.name

# Random shuffle corpora to ensure fairness of training and estimates.
def build_noisy_set(input, wrong_examples_file, double_linked_zipf_freqs=None, noisy_target_tokenizer=None):
    good_sentences  = TemporaryFile("w+")
    wrong_sentences = TemporaryFile("w+")
    total_size   = 0
    length_ratio = 0

    with TemporaryFile("w+") as temp:
        # (1) Calculate the number of lines, length_ratio, offsets
        offsets = []
        nline = 0
        ssource = 0
        starget = 0
        count = 0

        for line in input:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                offsets.append(count)
                count += len(bytearray(line, "UTF-8"))
                ssource += len(parts[0])
                starget += len(parts[1])
                nline += 1
                temp.write(line)

        temp.flush()

        total_size = nline
        n_aligned = total_size//2
        n_misaligned = total_size//2

        if total_size == 0:
            raise Exception("The input file {} is empty".format(input.name))
        elif not wrong_examples_file and  total_size < max(n_aligned, n_misaligned):
            raise Exception("Aborting... The input file {} has less lines than required by the numbers of good ({}) and wrong ({}) examples. Total lines required: {}".format(input.name, n_aligned, n_misaligned, n_aligned + n_misaligned))

        try:
            length_ratio = (ssource * 1.0)/(starget * 1.0) # It was (starget * 1.0)/(ssource * 1.0)
        except ZeroDivisionError:
            length_ratio = math.nan

        # (2) Get good sentences
        random.shuffle(offsets)

        for i in offsets[0:n_aligned]:
            temp.seek(i)
            good_sentences.write(temp.readline())

        # (3) Get wrong sentences
        if wrong_examples_file:
            # The file is already shuffled
            logging.info("Using wrong examples from file {} instead the synthetic method".format(wrong_examples_file.name))

            for i in wrong_examples_file:
                wrong_sentences.write(i)
        else:
            init_wrong_offsets = n_aligned+1
            end_wrong_offsets = min(n_aligned+n_misaligned, len(offsets))
            freq_noise_end_offset = n_aligned + int((end_wrong_offsets-n_aligned)/3)
            shuf_noise_end_offset = n_aligned + int(2 * (end_wrong_offsets-n_aligned) / 3)
            deletion_noise_end_offset = end_wrong_offsets
            if double_linked_zipf_freqs is not None:
                frequence_based_noise(init_wrong_offsets, freq_noise_end_offset, offsets, temp, wrong_sentences,
                                     double_linked_zipf_freqs, noisy_target_tokenizer)
            shuffle_noise(freq_noise_end_offset+1, shuf_noise_end_offset, offsets, temp, wrong_sentences)
            missing_words_noise(shuf_noise_end_offset+1, deletion_noise_end_offset, offsets, temp, wrong_sentences,
                                noisy_target_tokenizer)
        temp.close()

    good_sentences.seek(0)
    wrong_sentences.seek(0)

    return total_size, length_ratio, good_sentences, wrong_sentences

# Random shuffle corpora to ensure fairness of training and estimates.
def shuffle_noise(from_idx, to_idx, offsets, temp, wrong_sentences):
    random_idxs = list(range(from_idx, to_idx))
    random.shuffle ( random_idxs )
    sorted_idx = range(from_idx, to_idx)
    for sidx,tidx in zip(sorted_idx, random_idxs):
        temp.seek(offsets[sidx])
        line = temp.readline()
        parts = line.rstrip("\n").split("\t")
        sline = parts[0]

        temp.seek(offsets[tidx])
        line = temp.readline()
        parts = line.rstrip("\n").split("\t")
        tline = parts[1]

        wrong_sentences.write(sline)
        wrong_sentences.write("\t")
        wrong_sentences.write(tline)
        wrong_sentences.write("\n")

# Random shuffle corpora to ensure fairness of training and estimates.
def frequence_based_noise(from_idx, to_idx, offsets, temp, wrong_sentences, double_linked_zipf_freqs,
                         noisy_target_tokenizer):
    for i in offsets[from_idx:to_idx+1]:
        temp.seek(i)
        line = temp.readline()
        parts = line.rstrip("\n").split("\t")

        t_toks = noisy_target_tokenizer.tokenize(parts[1])

        parts[1] = noisy_target_tokenizer.detokenize(add_freqency_replacement_noise_to_sentence(t_toks, double_linked_zipf_freqs))
        wrong_sentences.write(parts[0])
        wrong_sentences.write("\t")
        wrong_sentences.write(parts[1])
        wrong_sentences.write("\n")

# Introduce noise to sentences using word frequence
def add_freqency_replacement_noise_to_sentence(sentence, double_linked_zipf_freqs):
    count = 0
    sent_orig = sentence[:]
    # Loop until any of the chosen words have an alternative, at most 3 times
    while True:
        # Random number of words that will be replaced
        num_words_replaced = random.randint(1, len(sentence))
        # Replacing N words at random positions
        idx_words_to_replace = random.sample(range(len(sentence)), num_words_replaced)

        for wordpos in idx_words_to_replace:
            w = sentence[wordpos]
            wfreq = double_linked_zipf_freqs.get_word_freq(w)
            alternatives = double_linked_zipf_freqs.get_words_for_freq(wfreq)
            if alternatives is not None:
                alternatives = list(alternatives)

                # Avoid replace with the same word
                if w.lower() in alternatives:
                    alternatives.remove(w.lower())
                if not alternatives == []:
                    sentence[wordpos] = random.choice(alternatives)
        count += 1
        if sentence != sent_orig:
            break
        elif count >= 3:
            return None

    return sentence


# Random shuffle corpora to ensure fairness of training and estimates.
def missing_words_noise(from_idx, to_idx, offsets, temp, wrong_sentences, noisy_target_tokenizer):
    for i in offsets[from_idx:to_idx+1]:
        temp.seek(i)
        line = temp.readline()
        parts = line.rstrip("\n").split("\t")
        t_toks = noisy_target_tokenizer.tokenize(parts[1])
        parts[1] = noisy_target_tokenizer.detokenize(remove_words_randomly_from_sentence(t_toks))
        wrong_sentences.write(parts[0])
        wrong_sentences.write("\t")
        wrong_sentences.write(parts[1])
        wrong_sentences.write("\n")

def remove_words_randomly_from_sentence(sentence):
    num_words_deleted = random.randint(1, len(sentence))
    idx_words_to_delete = sorted(random.sample(range(len(sentence)), num_words_deleted), reverse=True)
    for wordpos in idx_words_to_delete:
        del sentence[wordpos]
    return sentence

# Calculate precision, recall and accuracy over the 0.0,1.0,0.1 histogram of
# good and  wrong alignments
def precision_recall(hgood, hwrong):
    precision = []
    recall    = []
    accuracy  = []
    total = sum(hgood) + sum(hwrong)

    for i in range(len(hgood)):
        tp = sum(hgood[i:])   # true positives
        fp = sum(hwrong[i:])  # false positives
        fn = sum(hgood[:i])   # false negatives
        tn = sum(hwrong[:i])  # true negatives
        try:
            precision.append(tp*1.0/(tp+fp))     # precision = tp/(tp+fp)
        except ZeroDivisionError:
            precision.append(math.nan)
        try:
            recall.append(tp*1.0/(tp+fn))        # recall = tp/(tp+fn)
        except ZeroDivisionError:
            recall.append(math.nan)
        try:
            accuracy.append((tp+tn)*1.0/total)   # accuracy = (tp+tn) / total
        except ZeroDivisionError:
            accuracy.append(math.nan)

    return precision, recall, accuracy


def repr_right(numeric_list, numeric_fmt = "{:1.4f}"):
    result_str = ["["]
    for i in range(len(numeric_list)):
        result_str.append(numeric_fmt.format(numeric_list[i]))
        if i < (len(numeric_list)-1):
            result_str.append(", ")
        else:
            result_str.append("]")
    return "".join(result_str)


# Write YAML with the training parameters and quality estimates
def write_metadata(myargs, classifier, hgood, hwrong):
    out = myargs.metadata

    precision, recall, accuracy = precision_recall(hgood, hwrong)
    good_test_hist = "good_test_histogram: {}\n".format(hgood.__repr__())
    wrong_test_hist = "wrong_test_histogram: {}\n".format(hwrong.__repr__())
    precision_hist = "precision_histogram: {}\n".format(repr_right(precision))
    recall_hist = "recall_histogram: {}\n".format(repr_right(recall))
    accuracy_hist = "accuracy_histogram: {}\n".format(repr_right(accuracy))
    logging.debug(good_test_hist)
    logging.debug(wrong_test_hist)
    logging.debug(precision_hist)
    logging.debug(recall_hist)
    logging.debug(accuracy_hist)

    source_word_freqs = os.path.basename(myargs.source_word_freqs.name)
    target_word_freqs = os.path.basename(myargs.target_word_freqs.name)
    if myargs.porn_removal_file is not None:
        porn_removal_file = os.path.basename(myargs.porn_removal_file)

    # Writing it by hand (not using YAML libraries) to preserve the order
    out.write("source_lang: {}\n".format(myargs.source_lang))
    out.write("target_lang: {}\n".format(myargs.target_lang))
    out.write("source_word_freqs: {}\n".format(source_word_freqs))
    out.write("target_word_freqs: {}\n".format(target_word_freqs))
    out.write(good_test_hist)
    out.write(wrong_test_hist)
    out.write(precision_hist)
    out.write(recall_hist)
    out.write(accuracy_hist)

    if myargs.porn_removal_file is not None and myargs.porn_removal_train is not None:
        out.write("porn_removal_file: {}\n".format(porn_removal_file))
        out.write("porn_removal_side: {}\n".format(myargs.porn_removal_side))

    if myargs.source_tokenizer_command is not None:
        out.write("source_tokenizer_command: {}\n".format(myargs.source_tokenizer_command))
    if myargs.target_tokenizer_command is not None:
        out.write("target_tokenizer_command: {}\n".format(myargs.target_tokenizer_command))

    # Save vocabulary files if the classifier has them
    for file_attr in ['spm_file', 'wv_file', 'vocab_file']:
        if hasattr(classifier, file_attr):
            out.write(file_attr + f": {getattr(classifier, file_attr)}\n")
    # Save classifier
    out.write(f"classifier_file: {classifier.model_file}\n")
    out.write(f"classifier_type: {myargs.classifier_type}\n")

    # Save classifier train settings
    out.write("classifier_settings:\n")
    for key in sorted(classifier.settings.keys()):
        # Don't print objects
        if type(classifier.settings[key]) in [int, str]:
            out.write("    " + key + ": " + str(classifier.settings[key]) + "\n")

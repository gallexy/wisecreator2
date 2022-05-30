#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import shutil
import sqlite3
#import loggingpwd
import logging
import argparse
import subprocess

import nltk
#nltk.data.path.append("/Users/liu/nltk_data")
import cursor
from dataclasses import dataclass

from wisecreator import book as ww_book
from wisecreator.utils import get_path_to_mobitool, get_path_to_data

global wisewords_set

def check_dependencies():
    try:
        subprocess.check_output('ebook-convert --version', shell=True)
    except subprocess.CalledProcessError:
        raise ValueError("Calibre not installed")

    path_to_mobitool = get_path_to_mobitool()
    if not os.path.exists(path_to_mobitool):
        raise ValueError(path_to_mobitool + " not found")


class ProgressBarImpl:
    def __init__(self, total, prefix='', suffix='', decimals=1):
        self.total = total
        self.prefix = prefix
        self.suffix = suffix
        self.decimals = decimals
        self.bar_length = shutil.get_terminal_size()[0] // 2
        self.str_format = "{0:." + str(decimals) + "f}"
        self.iteration = 0

    def print_progress(self):
        percents = self.str_format.format(100 * (self.iteration / float(self.total)))
        filled_length = int(round(self.bar_length * self.iteration / float(self.total)))
        bar = '+' * filled_length + '-' * (self.bar_length - filled_length)
        progress_bar = "\r%s |%s| %s%s %s" % (self.prefix, bar, percents, '%', self.suffix)
        print(progress_bar, end='', flush=True)

    def increment(self):
        self.iteration += 1
        self.print_progress()


class ProgressBar:
    def __init__(self, total, prefix='', suffix='', decimal=1):
        self.pb = ProgressBarImpl(total, prefix, suffix, decimal)

    def __enter__(self):
        cursor.hide()
        self.pb.print_progress()
        return self.pb

    def __exit__(self, exc_type, exc_val, exc_tb):
        print("")
        cursor.show()


class WordFilter:
    def __init__(self, path_to_filter):
        with open(path_to_filter, 'rt') as f:
            self.do_not_take = []
            for line in f:
                if line.strip()[0] == '#':
                    continue
                word = line.strip()
                self.do_not_take.append(word)

    def is_take_word(self, word):
        word = word.lower()
        if word in self.do_not_take:
            return False

        # Do not take words with contractions
        # like "tree's", "he'll", "we've", e.t.c
        if word.find('\'') != -1:
            return False

        return True


class LangLayerInserter:
    def __init__(self, path_to_dir, book_asin):
        self.asin = book_asin
        self.conn = None
        self.cursor = None
        self.path_to_dir = path_to_dir
        self.open_db()
        try:
            query = "CREATE TABLE glosses ("\
                    "start INTEGER PRIMARY KEY,"\
                    "end INTEGER,"\
                    "difficulty INTEGER,"\
                    "sense_id INTEGER,"\
                    "low_confidence BOOLEAN)"
            self.cursor.execute(query)
            query = "CREATE TABLE metadata (key TEXT, value TEXT)"
            self.cursor.execute(query)
        except sqlite3.Error as e:
            print(e)

        en_dictionary_version = "2016-09-14"
        en_dictionary_revision = '57'
        en_dictionary_id = 'kll.en.en'

        metadata = {
            'acr': 'CR!W0W520HKPX6X12GRQ87AQC3XW3BV',
            'targetLanguages': 'en',
            'sidecarRevision': '45',
            'ftuxMarketplaces': 'ATVPDKIKX0DER,A1F83G8C2ARO7P,A39IBJ37TRP1C6,A2EUQ1WTGCTBG2',
            'bookRevision': 'b5320927',
            'sourceLanguage': 'en',
            'enDictionaryVersion': en_dictionary_version,
            'enDictionaryRevision': en_dictionary_revision,
            'enDictionaryId': en_dictionary_id,
            'sidecarFormat': '1.0',
        }

        try:
            for key, value in metadata.items():
                query = "INSERT INTO metadata VALUES (?, ?)"
                self.cursor.execute(query, (key, value))
            self.conn.commit()
        except sqlite3.Error as e:
            print(e)

    def open_db(self):
        if self.conn is None:
            db_name = "LanguageLayer.en.{}.kll".format(self.asin)
            path_to_db = os.path.join(self.path_to_dir, db_name)
            self.conn = sqlite3.connect(path_to_db)
            self.cursor = self.conn.cursor()

    def close_db(self):
        self.conn.close()
        self.conn = None

    def start_transaction(self):
        self.cursor.execute("BEGIN TRANSACTION")

    def end_transaction(self):
        self.conn.commit()

    def add_gloss(self, start, difficulty, sense_id):
        self.open_db()
        try:
            query = "INSERT INTO glosses VALUES (?,?,?,?,?)"
            new_gloss = (start, None, difficulty, sense_id, 0)
            self.cursor.execute(query, new_gloss)
        except sqlite3.Error:
            pass


class LanguageLayerDb:
    def __init__(self, path_to_dir, book_asin):
        self.inserter = LangLayerInserter(path_to_dir, book_asin)

    def __enter__(self):
        self.inserter.start_transaction()
        return self.inserter

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.inserter.end_transaction()
        self.inserter.close_db()


@dataclass
class Sense:
    id: int
    difficulty: int


class SenseProvider:
    def __init__(self, path_to_senses):
        def is_phrase(target_line):
            if target_line[0] == '"':
                return True
            return False

        self.senses = {}
        with open(path_to_senses, 'rb') as f:
            f = f.read().decode('utf-8')
            for line in f.splitlines():
                line = line.strip()
                if is_phrase(line):
                    continue
                word, sense_id, difficulty = line.split(',')
                self.senses[word] = Sense(sense_id, difficulty)

    def get_sense(self, word):
        if word in self.senses:
            return self.senses[word]
        else:
            return None


class WordProcessor:
    def __init__(self, path_to_nltk_data, word_filter, sense_provider):
        # nltk.data.path = [path_to_nltk_data] + nltk.data.path
        self.lemmatizer = nltk.WordNetLemmatizer()
        self.sense_provider = sense_provider
        self.word_filter = word_filter

    def get_lemma(self, word):
        def get_part_of_speech(target_word):
            pos = nltk.pos_tag([target_word])[0][1][0]

            if pos == 'J':
                return nltk.corpus.wordnet.ADJ
            if pos == 'V':
                return nltk.corpus.wordnet.VERB
            if pos == 'N':
                return nltk.corpus.wordnet.NOUN
            if pos == 'R':
                return nltk.corpus.wordnet.ADV

            return nltk.corpus.wordnet.NOUN

        word = word.lower()
        return self.lemmatizer.lemmatize(word, pos=get_part_of_speech(word))

    def get_sense(self, word):
        if self.word_filter.is_take_word(word):
            return self.sense_provider.get_sense(self.get_lemma(word))
        else:
            return None


class WWResult:
    def __init__(self, input_path, output_path):
        if not os.path.exists(input_path):
            print("[-] Wrong path to book: {}".format(input_path))

        self._input_file_name = os.path.basename(input_path)
        self._output_path = output_path

        self.book_name = os.path.splitext(self._input_file_name)[0]
        self.result_dir_path = self._get_result_dir_path()
        self.book_path = os.path.join(self.result_dir_path, self._input_file_name)

        sdr_dir_name = "{}.sdr".format(self.book_name)
        self.sdr_dir_path = os.path.join(self.result_dir_path, sdr_dir_name)
        os.makedirs(self.sdr_dir_path)

        shutil.copyfile(input_path, self.book_path)

    def _get_result_dir_path(self):
        dir_name = "{}-WordWised".format(self.book_name)
        result = os.path.join(self._output_path, dir_name)

        if os.path.exists(result):
            shutil.rmtree(result)

        if not os.path.exists(result):
            os.makedirs(result)

        return result


class WordWiser:
    def __init__(self, word_processor):
        self.word_processor = word_processor

    def get_logger_for_words(self):
        wlog = logging.getLogger('word-processing')
        wlog.setLevel(logging.DEBUG)
        fh = logging.FileHandler('result_senses.log')
        wlog.addHandler(fh)
        return wlog

    def process_glosses(self, lldb, wlog, glosses):
        global wisewords_set
        for gloss in glosses:
            sense = self.word_processor.get_sense(gloss.word)
            if sense:
                wisewords_set.add(gloss.word.lower())
                wlog.debug("{} - {} - {}".format(gloss.offset, gloss.word, sense.id))
                lldb.add_gloss(gloss.offset, sense.difficulty, sense.id)
            yield gloss

    def wordwise(self, path_to_book, output_path):
        target = WWResult(path_to_book, output_path)
        book = ww_book.Book(target.book_path, get_path_to_mobitool())
        book_asin = book.get_or_create_asin()
        glosses = book.get_glosses()

        if len(glosses) == 0:
            print("[.] There are no suitable words in the book")
            return

        print("[.] Count of words: {}".format(len(glosses)))

        wlog = self.get_logger_for_words()
        with LanguageLayerDb(target.sdr_dir_path, book_asin) as lldb:
            with ProgressBar(len(glosses), "[.] Processing words: ") as pb:
                for gloss in self.process_glosses(lldb, wlog, glosses):
                    pb.increment()

        print("[.] Success!")
        print("Now copy this folder: \"{}\" to your Kindle".format(target.result_dir_path))


def process(path_to_book, output_path):
    path_to_script = os.path.dirname(os.path.realpath(__file__))
    path_to_nltk_data = os.path.join(path_to_script, "nltk_data")
#    path_to_nltk_data = '/Users/liu/nltk_data'
    word_processor = WordProcessor(path_to_nltk_data,
                                   WordFilter(get_path_to_data("filter.txt")),
                                   SenseProvider(get_path_to_data("senses.csv")))

    ww = WordWiser(word_processor)
    ww.wordwise(path_to_book, output_path)


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("fi", type=str, metavar="PATH_TO_YOUR_BOOK")
    if len(sys.argv)==1:
        file_args='gatsby.mobi'
        file_fullname='/Users/liuxiaohua/pyproject/wisecreator2/testbooks/'+file_args   
        args = parser.parse_args([file_fullname])
    else:
        args = parser.parse_args()


    print("[.] Checking dependencies")
    try:
        check_dependencies()
    except ValueError as e:
        print("  [-] Checking failed:")
        print("    |", e)
        return

    global wisewords_set
    wisewords_set=set()
    process(args.fi, os.path.dirname(args.fi))

    sortedwords=list(wisewords_set)
    sortedwords.sort()
    print(sortedwords,len(sortedwords))

if __name__ == "__main__":
    main()

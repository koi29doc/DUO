import os
import random
import subprocess
import re
import zipfile
import datetime

import numpy as np
import pandas as pd
import chardet

import logging
log = logging.getLogger(__name__)

def raise_on_duplicates(input_list):
    """
    Verify if a list as duplicated values.

    Raised DuplicateColumnName exception if this is the cas.
    """
    if len(input_list) != len(set(input_list)):
        col_names = ["'{}'".format(name) for name in input_list]
        raise DuplicateColumnName(
                ("Found duplicate column names:\n"
                 "{}\n"
                 "Please clean your header"
                ).format(" ".join(col_names)) )


class CastFloat64(Exception):
    """
    Exception raised when data column cannot be casted to float64
    """

    def __init__(self, message):
        self.message = message


class DuplicateColumnName(Exception):
    """
    Exception raised when column names are identical
    """

    def __init__(self, message):
        self.message = message


class Input():
    """
    Class to handle autoclass input files and parameters
    """

    def __init__(self, inputfolder='',
                        missing_encoding="?",
                        separator="\t",
                        tolerate_error=False):
        """
        Object instanciation
        """
        self.inputfolder = inputfolder
        self.missing_encoding = missing_encoding
        self.separator = separator

        self.input_datasets = []
        self.full_dataset = Dataset()

        self.tolerate_error = tolerate_error
        self.had_error = False

        #self.change_working_dir()


    def handle_error(f):
        """
        Handle error during data parsing and formating
        """
        def try_function(self, *args, **kwargs):
            if self.tolerate_error or not self.had_error:
                try:
                    return f(self, *args, **kwargs)
                except Exception as e:
                    for line in str(e).split('\n'):
                        log.error(line)
                    self.had_error = True
        return try_function


    @handle_error
    def change_working_dir(self):
        """
        change working dir
        """
        log.info("Changing working directory")
        os.chdir(self.inputfolder)


    @handle_error
    def add_input_data(self, input_file, input_type, input_error=None):
        """
        Add input data for clustering
        """
        dataset = Dataset()
        # verify data type
        assert input_type in ['real scalar', 'real location', 'discrete'], \
               ("data type in {} should be: "
                "'real scalar', 'real location' or 'discrete'"
                .format(input_file))
        msg = "Reading data file '{}' as '{}'".format(input_file, input_type)
        if input_type in ['real scalar', 'real location']:
            msg += " with error {}".format(input_error)
        log.info(msg)
        dataset.read_datafile(input_file, input_type, input_error)
        dataset.clean_column_names()
        dataset.check_data_type()
        self.input_datasets.append(dataset)


    @handle_error
    def merge_dataframes(self):
        """
        Merge input dataframes from datasets

        Dataframes are merged based on an 'outer' join
        https://pandas.pydata.org/pandas-docs/stable/merging.html
        - all lines are kept
        - missing data might appear
        """
        # merge dataframes and column meta data
        if len(self.input_datasets) == 1:
            self.full_dataset = self.input_datasets[0]
        else:
            log.info("Merging input data")
            df_lst = []
            for dataset in self.input_datasets:
                df_lst.append(dataset.df)
                self.full_dataset.column_meta = {**self.full_dataset.column_meta, **dataset.column_meta}
            self.full_dataset.df = pd.concat(df_lst, axis=1, join="outer")
        # check for identical column names
        raise_on_duplicates(self.full_dataset.df.columns)

        nrows, ncols = self.full_dataset.df.shape
        log.info("Final dataframe has {} lines and {} columns"
                     .format(nrows, ncols+1))
        self.full_dataset.search_missing_values()


    @handle_error
    def create_db2_file(self, filename="clust"):
        """
        Create .db2 file
        """
        db2_name = filename + ".db2"
        tsv_name = filename + ".tsv"
        log.info("Writing {} file".format(db2_name))
        log.info("If any, missing values will be encoded as '{}'"
                     .format(self.missing_encoding))
        self.full_dataset.df.to_csv("clust.db2",
                                    header=False,
                                    sep=self.separator,
                                    na_rep=self.missing_encoding)
        log.debug("Writing {} file [for later use]".format(tsv_name))
        self.full_dataset.df.to_csv("clust.tsv",
                                    header=True,
                                    sep=self.separator,
                                    na_rep="")


    @handle_error
    def create_hd2_file(self):
        """
        Create .hd2 file
        """
        log.info("Writing .hd2 file")
        column_names = self.full_dataset.df.columns
        with open("clust.hd2", "w") as hd2:
            hd2.write("num_db2_format_defs {}\n".format(2))
            hd2.write("\n")
            # get number of columns + index
            hd2.write("number_of_attributes {}\n".format(len(column_names)+1))
            hd2.write("separator_char '{}'\n".format(self.separator))
            hd2.write("\n")
            # write first columns (protein/gene names)
            hd2.write('0 dummy nil "{}"\n'.format(self.full_dataset.df.index.name))
            for idx, name in enumerate(column_names):
                meta = self.full_dataset.column_meta[name]
                if meta["type"] == "real scalar":
                    # by default minimum value is set to 0.0
                    assert self.full_dataset.df.min()[idx] >= 0.0, \
                           "min value for {} shoud be >= 0.0".format(name)
                    hd2.write('{} real scalar "{}" zero_point 0.0 rel_error {}\n'
                              .format(idx+1,
                                      name,
                                      meta["error"])
                             )
                if meta["type"] == "real location":
                    hd2.write('{} real location "{}" error {}\n'
                              .format(idx+1,
                                      name,
                                      meta["error"])
                             )
                if meta["type"] == "discrete":
                    hd2.write('{} discrete nominal "{}" range {}\n'
                              .format(idx+1,
                                      name,
                                      self.full_dataset.df[name].nunique())
                             )


    @handle_error
    def create_model_file(self):
        """
        Create .model file
        """
        log.info("Writing .model file")
        # available models:
        real_values_normals = []
        real_values_missing = []
        multinomial_values  = []
        # assign column data to models
        for col_idx, col_name in enumerate(self.full_dataset.df.columns):
            meta = self.full_dataset.column_meta[col_name]
            if meta['type'] in ['real scalar', 'real location']:
                if not meta['missing']:
                    real_values_normals.append(str(col_idx+1))
                else:
                    real_values_missing.append(str(col_idx+1))
            if meta['type'] == 'discrete':
                multinomial_values.append(str(col_idx+1))
        # count number of different models used
        # the first model is the one with labels (first column)
        models_count = 1
        for model in [real_values_normals, real_values_missing, multinomial_values]:
            if model:
                models_count += 1
        # write model file
        with open("clust.model", "w") as model:
            model.write("model_index 0 {}\n".format(models_count))
            model.write("ignore 0\n")
            if real_values_normals:
                model.write("single_normal_cn {}\n"
                            .format(" ".join(real_values_normals)))
            if real_values_missing:
                model.write("single_normal_cm {}\n"
                            .format(" ".join(real_values_missing)))
            if multinomial_values:
                model.write("single_multinomial {}\n"
                            .format(" ".format(multinomial_values)))


    @handle_error
    def create_sparams_file(self,
                            max_duration=3600,
                            max_n_tries=200,
                            max_cycles=1000):
        """
        Create .s-params file
        """
        log.info("Writing .s-params file")
        with open("clust.s-params", "w") as sparams:
            sparams.write("screen_output_p = false \n")
            sparams.write("break_on_warnings_p = false \n")
            sparams.write("force_new_search_p = true \n")

            # max_duration
            # When > 0, specifies the maximum number of seconds to run.
            # When = 0, allows run to continue until otherwise halted.
            # doc in search-c.text, lines 493-495
            # default value: max_duration = 0
            # max_duration set to 3600 sec. (1 hour)
            sparams.write("max_duration = {} \n".format(max_duration))

            # max_n_tries
            # max number of trials
            # doc in search-c.text, lines 403-404
            # default value: max_n_tries = 200
            sparams.write("max_n_tries = {} \n".format(max_n_tries))

            # max_cycles
            # max number of cycles per trial
            # doc in search-c.text, lines 316-317
            # default value: max_cyles = 200
            sparams.write("max_cycles = {} \n".format(max_cycles))

            # start_j_list
            # initial guess of the number of clusters
            # doc in search-c.text, line 332
            # default values: 2, 3, 5, 7, 10, 15, 25
            sparams.write('start_j_list = 2, 3, 5, 7, 10, 15, 25, 35, 45, 55, 65, 75, 85, 95, 105 \n')


    @handle_error
    def create_rparams_file(self):
        """
        Create .r-params file
        """
        log.info("Writing .r-params file")
        with open("clust.r-params", "w") as rparams:
            rparams.write('xref_class_report_att_list = 0, 1, 2 \n')
            rparams.write('report_mode = "data" \n')
            rparams.write('comment_data_headers_p = true \n')


    @handle_error
    def create_run_file(self):
        """
        Create .sh file

        autoclass executable must be in the PATH
        """
        log.info("Writing run file")
        with open('run_autoclass.sh', 'w') as runfile:
            runfile.write("autoclass -search clust.db2 clust.hd2 clust.model clust.s-params \n")
            runfile.write("autoclass -reports clust.results-bin clust.search clust.r-params \n")


    @handle_error
    def create_run_file_test(self):
        """
        Create .sh file
        """
        log.info("Writing run file")
        with open('run_autoclass.sh', 'w') as runfile:
            runfile.write("for a in $(seq 1 60) \n")
            runfile.write("do \n")
            runfile.write("sleep 1 \n")
            runfile.write("touch clust.log \n")
            runfile.write("done \n")
            runfile.write("touch clust.rlog \n")


    @handle_error
    def prepare_input_files(self):
        """
        Prepare input and parameters files
        """
        log.info("Preparing data and parameters files")
        self.change_working_dir()
        self.create_db2_file()
        self.create_hd2_file()
        self.create_model_file()
        self.create_sparams_file()
        self.create_rparams_file()
        self.create_run_file()


    @handle_error
    def run(self, tag=""):
        """
        Run autoclass
        """
        log.info("Running clustering...")
        proc = subprocess.Popen(['bash', 'run_autoclass.sh', tag])
        return True


    @handle_error
    def print_files(self):
        """
        Print generated files
        """
        content = ""
        for name in ('clust.hd2', 'clust.model', 'clust.s-params', 'clust.r-params', 'run_autoclass.sh'):
            if os.path.exists(name):
                content += "\n"
                content += "--------------------------------------------------------------------------\n"
                content += "{}\n".format(name)
                content += "--------------------------------------------------------------------------\n"
                with open(name, 'r') as param_file:
                    content += "".join( param_file.readlines() )
        return content


class Dataset():
    """
    Class to handle autoclass data files
    """


    def __init__(self):
        """
        Object instantiation
        """
        # filename of input_file
        self.input_file = ""
        # Pandas dataframe with data
        self.df = None
        # Column meta data: data type, error, missing values
        self.column_meta = {}


    def load(self):
        """
        Load data
        """
        self.read_datafile()
        self.clean_column_names()
        self.check_data_type()
        self.check_missing_values()



    def check_duplicate_col_names(self, input_file):
        """
        Check duplicate column clean_column_names
        """
        with open(input_file) as f_in:
            header = f_in.readline().strip().split("\t")
            raise_on_duplicates(header)


    def read_datafile(self, input_file='', data_type='', error=None):
        """
        Read data file as pandas dataframe

        Header is on first row (header=0)
        Gene/protein/orf names are on first column (index_col=0)
        """
        # verify data type
        assert data_type in ['real scalar', 'real location', 'discrete'], \
               ("data type in {} should be: "
                "'real scalar', 'real location' or 'discrete'"
                .format(input_file))
        # check for duplicate column names
        self.input_file = input_file
        self.check_duplicate_col_names(input_file)
        # guess encoding
        with open(input_file, 'rb') as f:
            enc_result = chardet.detect(f.read())
        log.info("Detected encoding: {}".format(enc_result['encoding']))
        # load data
        self.df = pd.read_table(input_file, sep='\t',
                                            header=0,
                                            index_col=0,
                                            encoding=enc_result['encoding'])
        nrows, ncols = self.df.shape
        # save column meta data (data type, error, missing values)
        for col in self.df.columns:
            meta = {'type': data_type,
                    'error': error,
                    'missing': False}
            self.column_meta[col] = meta
        log.info("Found {} rows and {} columns"
                     .format(nrows, ncols+1))


    def clean_column_names(self):
        """
        Cleanup column names
        """
        regex = re.compile('[^A-Za-z0-9 .+-]+')
        log.debug("Checking column names")
        # check index column name first
        col_name = self.df.index.name
        col_name_new = regex.sub("_", col_name)
        if col_name_new != col_name:
            self.df.index.name = col_name_new
            log.warning("Column '{}' renamed to '{}'"
                            .format(col_name, col_name_new))
        # then other column names
        for col_name in self.df.columns:
            col_name_new = regex.sub("_", col_name)
            if col_name_new != col_name:
                self.df.rename(columns={col_name: col_name_new}, inplace=True)
                log.warning("Column '{}' renamed to '{}'"
                                .format(col_name, col_name_new))
                # update column meta data
                self.column_meta[col_name_new] = self.column_meta.pop(col_name)
        # print all columns names
        log.debug("Index name {}'".format(self.df.index.name))
        for name in self.df.columns:
            log.debug("Column name '{}'".format(name))


    def check_data_type(self):
        """
        Check data type
        """
        log.info("Checking data format")
        for col in self.df.columns:
            if self.column_meta[col]['type'] in ['real scalar', 'real location']:
                try:
                    self.df[col].astype('float64')
                    log.info("Column '{}'\n".format(col)
                                 +self.df[col].describe(percentiles=[]).to_string())
                except:
                    raise CastFloat64(("Cannot cast column '{}' to float\n"
                                       "Check your input file!").format(col)
                                     )
            if self.column_meta[col]['type'] == "discrete":
                log.info("Column '{}'\n{} different values"
                             .format(col, self.df[col].nunique())
                             )


    def search_missing_values(self):
        """
        Search for missing values
        """
        log.info('Searching for missing values')
        columns_with_missing = self.df.columns[ self.df.isnull().any() ].tolist()
        if columns_with_missing:
            for col in columns_with_missing:
                self.column_meta[col]['missing'] = True
            log.warning('Missing values found in columns: {}'
                            .format(" ".join(columns_with_missing)))
        else:
            log.info('No missing values found')
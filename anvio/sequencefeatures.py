# -*- coding: utf-8
# pylint: disable=line-too-long

"""Classes to deal with sequence features"""

import os
import argparse
import xml.etree.ElementTree as ET

from numba import jit

import anvio
import anvio.utils as utils
import anvio.dbops as dbops
import anvio.terminal as terminal
import anvio.filesnpaths as filesnpaths

from anvio.errors import ConfigError
from anvio.drivers.blast import BLAST


__author__ = "Developers of anvi'o (see AUTHORS.txt)"
__copyright__ = "Copyleft 2015-2021, the Meren Lab (http://merenlab.org/)"
__credits__ = []
__license__ = "GPL 3.0"
__version__ = anvio.__version__
__maintainer__ = "A. Murat Eren"
__email__ = "a.murat.eren@gmail.com"


P = terminal.pluralize
pp = terminal.pretty_print
run_quiet = terminal.Run(verbose=False)
progress_quiet = terminal.Progress(verbose=False)


class Palindrome:
    def __init__(self, sequence_name=None, first_start=None, first_end=None, first_sequence=None, second_start=None,
                 second_end=None, second_sequence=None, num_mismatches=None, length=None, distance=None, num_gaps=None,
                 midline='', method=None, run=terminal.Run()):
        self.run = run
        self.sequence_name = sequence_name
        self.first_start = first_start
        self.first_end = first_end
        self.first_sequence = first_sequence
        self.second_start = second_start
        self.second_end = second_end
        self.second_sequence = second_sequence
        self.num_mismatches = num_mismatches
        self.length = length
        self.distance = distance
        self.num_gaps = num_gaps
        self.midline = midline
        self.method = method


    def __str__(self):
        return f"Len: {self.length}; Dist: {self.distance}; {self.first_sequence} ({self.first_start}:{self.first_end}) :: {self.second_sequence} ({self.second_start}:{self.second_end})"


    def display(self):

        # we don't care what `verbose` variable the original instance may have. if
        # the user requests to `display` things, we will display it, and then store
        # the original state again.
        verbose = self.run.verbose
        self.run.verbose = True

        self.run.warning(None, header=f'{self.length} nts palindrome', lc='yellow')
        self.run.info('Method', self.method, mc='green')
        self.run.info('1st sequence [start:stop]', f"[{self.first_start}:{self.first_end}]", mc='green')
        self.run.info('2nd sequence [start:stop]', f"[{self.second_start}:{self.second_end}]", mc='green')
        self.run.info('Number of mismatches', f"{self.num_mismatches}", mc='red')
        self.run.info('Distance between', f"{self.distance}", mc='yellow')
        self.run.info('1st sequence', self.first_sequence, mc='green')
        self.run.info('ALN', self.midline, mc='green')
        self.run.info('2nd sequence', self.second_sequence, mc='green')

        # store the original verbose state
        self.run.verbose = verbose


class Palindromes:
    def __init__(self, args=argparse.Namespace(), run=terminal.Run(), progress=terminal.Progress()):
        self.args = args
        self.run = run
        self.progress = progress

        A = lambda x: args.__dict__[x] if x in args.__dict__ else None
        self.palindrome_search_algorithm = A('palindrome_search_algorithm')
        self.min_palindrome_length = 10 if A('min_palindrome_length') == None else A('min_palindrome_length')
        self.max_num_mismatches = A('max_num_mismatches') or 0
        self.min_distance = A('min_distance') or 0
        self.min_mismatch_distance_to_first_base = A('min_mismatch_distance_to_first_base') or 1
        self.verbose = A('verbose') or False
        self.contigs_db_path = A('contigs_db')
        self.fasta_file_path = A('fasta_file')
        self.output_file_path = A('output_file')

        self.palindrome_search_algorithms = {
            'BLAST': self._find_BLAST,
            'numba': self._find_numba,
        }

        self.num_threads = int(A('num_threads')) if A('num_threads') else 1
        self.blast_word_size = A('blast_word_size') or 10

        self.translate = {'A': 'T', 'T': 'A', 'C': 'G', 'G': 'C'}

        self.sanity_check()

        self.run.warning(None, header="SEARCH SETTINGS", lc="green")
        self.run.info('Minimum palindrome length', self.min_palindrome_length)
        self.run.info('Number of mismatches allowed', self.max_num_mismatches)
        self.run.info('Minimum gap length', self.min_distance)
        self.run.info('Be verbose?', 'No' if not self.verbose else 'Yes', nl_after=1)
        self.run.info('Number of threads for BLAST', self.num_threads)
        self.run.info('BLAST word size', self.blast_word_size, nl_after=1)

        self.user_is_warned_for_potential_performance_issues = False

        self.palindromes = {}


    def sanity_check(self):
        if self.contigs_db_path and self.fasta_file_path:
            raise ConfigError("You should either choose a FASTA file or a contigs db to send to this "
                              "class, not both :/")

        if self.min_mismatch_distance_to_first_base < 1:
            raise ConfigError("The minimum mismatch thistance to the first base from either of the palindrome "
                              "must be greater than 0.")

        if self.output_file_path:
            filesnpaths.is_output_file_writable(self.output_file_path, ok_if_exists=False)

        if self.contigs_db_path:
            utils.is_contigs_db(self.contigs_db_path)

        if self.fasta_file_path:
            filesnpaths.is_file_fasta_formatted(self.fasta_file_path)

        try:
            self.min_palindrome_length = int(self.min_palindrome_length)
        except:
            raise ConfigError("Minimum palindrome length must be an integer.")

        try:
            self.max_num_mismatches = int(self.max_num_mismatches)
        except:
            raise ConfigError("Maximum number of mismatches must be an integer.")

        if self.blast_word_size < 4:
            raise ConfigError("For everyone's sake, we set the minimum value for the minimum word size for BLAST to "
                              "5. If you need this to change, please let us know (or run the same command with `--debug` "
                              "flag, find the location of this control, and hack anvi'o by replacing that 4 with something "
                              "smaller -- anvi'o doesn't mind being hacked).")

        if self.min_palindrome_length < 4:
            raise ConfigError("For everyone's sake, we set the minimum value for the minimum palindrome length to "
                              "4. You have a problem with that? WELL, WELCOME TO THE CLUB, YOU'LL FIT RIGHT IN -- "
                              "WE HAVE A PROBLEM WITH LOGIC TOO.")


    def process(self):
        """Processes all sequences in a given contigs database or a FASTA file.

        What this function does depends on the configuration of the class. Member functions `find_gapless`
        or `find_with_gaps` may be more appropriate to call if there is a single sequence to process.
        """

        if self.contigs_db_path:
            contigs_db = dbops.ContigsDatabase(self.contigs_db_path)
            contig_sequences_dict = contigs_db.db.get_table_as_dict(anvio.tables.contig_sequences_table_name)

            self.progress.new('Searching', progress_total_items=len(contig_sequences_dict))
            for sequence_name in contig_sequences_dict:
                self.progress.update(f"{sequence_name} ({pp(len(contig_sequences_dict[sequence_name]['sequence']))} nts)", increment=True)
                self.find(contig_sequences_dict[sequence_name]['sequence'], sequence_name=sequence_name)
            self.progress.end()

        elif self.fasta_file_path:
            num_sequences = utils.get_num_sequences_in_fasta(self.fasta_file_path)
            fasta = anvio.fastalib.SequenceSource(self.fasta_file_path)
            self.progress.new('Searching', progress_total_items=num_sequences)

            while next(fasta):
                self.progress.update(f"{fasta.id} ({pp(len(fasta.seq))} nts)", increment=True)
                self.find(fasta.seq, sequence_name=fasta.id)
            self.progress.end()

        else:
            raise ConfigError("You called the `process` function of the class `Palindromes` without a FASTA "
                              "file or contigs database to process :(")

        self.report()


    def set_palindrome_search_algorithm(self, sequence):
        if len(sequence) >= 5000:
            return self._find_BLAST
        else:
            return self._find_numba


    def find(self, sequence, sequence_name="N/A", display_palindromes=False, **kwargs):
        """Find palindromes in a single sequence, and populate `self.palindromes`

        This method finds palindromes by delegating to either `_find_BLAST` and `_find_numba`.

        Notes
        =====
        - The method `process` may be a better one to call if you have an `args` object. See `anvi-search-palindromes`
          for example usage.
        """

        if sequence_name in self.palindromes:
            raise ConfigError(f"The sequence '{sequence_name}' is already in `self.palindromes`.")
        else:
            self.palindromes[sequence_name] = []

        sequence_length = len(sequence)
        if sequence_length < self.min_palindrome_length * 2 + self.min_distance:
            self.progress.reset()
            if sequence_name == 'N/A':
                friendly_sequence_name = "The sequence you have provided"
            else:
                friendly_sequence_name = f"The sequence '{sequence_name}'"
            self.run.warning(f"{friendly_sequence_name} is only {sequence_length} nts long, and so it is too "
                             f"short to find any palindromes in it that are at least {self.min_palindrome_length} nts with "
                             f"{self.min_distance} nucleoties in between :/ Anvi'o will most likely skip it.")

        method = self.palindrome_search_algorithms.get(self.palindrome_search_algorithm, self.set_palindrome_search_algorithm(sequence))
        palindromes = method(sequence, **kwargs)

        for palindrome in palindromes:
            if anvio.DEBUG or display_palindromes or self.verbose:
                self.progress.reset()
                palindrome.display()

            palindrome.sequence_name = sequence_name
            self.palindromes[sequence_name].append(palindrome)


    def _find_BLAST(self, sequence):
        """Find palindromes in a single sequence using BLAST

        This method of palindrome finding is slow for short sequences, but scales very well with
        arbitrarily sized palindrome searching, e.g. entire assemblies or genomes. If the sequence
        length > 5000, this is a great choice.

        Here are the timings as a function of sequence length.

            length 100:     51.7 ms ± 2.34 ms per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 278:     48.4 ms ± 947 µs per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 774:     47.9 ms ± 570 µs per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 2154:    49.8 ms ± 541 µs per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 5994:    52 ms ± 2.53 ms per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 16681:   51.7 ms ± 555 µs per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 46415:   56.4 ms ± 623 µs per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 129154:  63.8 ms ± 510 µs per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 359381:  100 ms ± 848 µs per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 1000000: 324 ms ± 4.95 ms per loop (mean ± std. dev. of 7 runs, 1 loop each)

        Returns
        =======
        out : list of anvio.sequencefeatures.Palindrome
            Returns a list of Palindrome objects. If no palindromes are found, empty list is returned.
        """
        palindromes = []
        sequence = sequence.upper()

        # setup BLAST job
        BLAST_search_tmp_dir = filesnpaths.get_temp_directory_path()
        fasta_file_path = os.path.join(BLAST_search_tmp_dir, 'sequence.fa')
        log_file_path = os.path.join(BLAST_search_tmp_dir, 'blast-log.txt')
        results_file_path = os.path.join(BLAST_search_tmp_dir, 'hits.xml')
        with open(fasta_file_path, 'w') as fasta_file:
            fasta_file.write(f'>sequence\n{sequence}\n')

        # run blast
        blast = BLAST(fasta_file_path, search_program='blastn', run=run_quiet, progress=progress_quiet)
        blast.evalue = 10
        blast.num_threads = self.num_threads
        blast.min_pct_id = 100 - self.max_num_mismatches
        blast.search_output_path = results_file_path
        blast.log_file_path = log_file_path
        blast.makedb(dbtype='nucl')

        if self.min_palindrome_length < 20 and len(sequence) > 10000 and not self.user_is_warned_for_potential_performance_issues:
            self.progress.reset()
            self.run.warning(f"Please note, you are searching for palindromes that are as short as {self.min_palindrome_length} "
                             f"in a sequence that is {pp(len(sequence))} nts long. If your palindrome search takes a VERY long time "
                             f"you may want to go for longer palindromes by setting a different `--min-palindrome-length` parameter "
                             f"and by increasing the BLAST word size using `--blast-word-size` parameter (please read the help menu first). "
                             f"This part of the code does not know if you have many more seqeunces to search, but anvi'o will not "
                             f"continue displaying this warning for additional seqeunces to minimize redundant informatio in your "
                             f"log files (because despite the popular belief anvi'o can actually sometimes be like nice and all).",
                             header="ONE-TIME PERFORMANCE WARNING")
            self.user_is_warned_for_potential_performance_issues = True

        blast.blast(outputfmt='5', word_size=self.blast_word_size, strand='minus')

        # parse the BLAST XML output
        root = ET.parse(blast.search_output_path).getroot()
        for query_sequence_xml in root.findall('BlastOutput_iterations/Iteration'):
            for hit_xml in query_sequence_xml.findall('Iteration_hits/Hit'):

                for hsp_xml in hit_xml.findall('Hit_hsps/Hsp'):
                    p = Palindrome(method='BLAST', run=self.run)

                    p.first_start = int(hsp_xml.find('Hsp_query-from').text) - 1
                    p.first_end = int(hsp_xml.find('Hsp_query-to').text)
                    p.first_sequence = hsp_xml.find('Hsp_qseq').text

                    p.second_start = int(hsp_xml.find('Hsp_hit-to').text) - 1
                    p.second_end = int(hsp_xml.find('Hsp_hit-from').text)
                    p.second_sequence = hsp_xml.find('Hsp_hseq').text

                    # Calculating the 'distance' next. But it is a bit tricky. Imagine this as your genomic context for
                    # this 'in-place' palindrome:
                    #
                    #    >>> 0        1
                    #    >>> 1234567890
                    #    >>> ...TCGA...
                    #
                    # where you indeed have a proper palindrome here. the start and end of both sequences of this
                    # palindrome will be the same: TCGA (3:7) :: TCGA (3:7). In this case, we can't simply calculate
                    # 'distance' by substracting the start of the second sequence from the end of the first, OR we
                    # can't simply remove it from our consideration because p.second_start - p.first_end is a negative
                    # value.
                    #
                    # In contrast, consider this as your genomic context for this 'distance palindrome':
                    #
                    #    >>> 0        1
                    #    >>> 12345678901234567
                    #    >>> ...ATCC...GGAT...
                    #
                    # This also is a proper palindrome. But the start and the end of each sequence will be different this
                    # time in the BLAST results: ATCC (3:7) :: ATCC (10:14). And for such distant palindromes, BLAST results
                    # will ALWAYS include the same result for its reverse complement, where p.second_start - p.first_end will
                    # be negative, which we will want to remove. So the following few lines consider all these scenarios
                    # to not always remove 'in-place' palindromes.

                    if p.first_start == p.second_start:
                        # this is an in-place palindrome. which means, the distance
                        # between these sequences is 0 and we have to manually set it
                        p.distance = 0
                    else:
                        # this is a distant palindrome, so we calculate the distance
                        # from actual positions:
                        p.distance = p.second_start - p.first_end

                    # for each distant palindrome in the sequence, there will be a copy of the reverse complement of the first
                    # hit. now we have set the distance properly, we can remove those from hits to be considered:
                    if p.distance < 0:
                        continue

                    # time to check the remaining ones for minimum distance, if it is defined:
                    if p.distance < self.min_distance:
                        continue

                    # before we continue, we will test for a special case: internal palindromes
                    # within larger palindromes of 0 distance. IT DOES HAPPEN I PROM.
                    if p.distance == 0:
                        internal_palindrome = False
                        for _p in palindromes:
                            if p.first_start > _p.first_start and p.first_start < _p.first_end:
                                internal_palindrome = True
                                break

                        if internal_palindrome:
                            continue

                    p.length = int(hsp_xml.find('Hsp_align-len').text)

                    if p.length < self.min_palindrome_length:
                        # buckle your seat belt Dorothy, 'cause Kansas is going bye-bye:
                        continue

                    p.num_gaps = int(hsp_xml.find('Hsp_gaps').text)
                    p.num_mismatches = int(hsp_xml.find('Hsp_align-len').text) - int(hsp_xml.find('Hsp_identity').text)
                    p.midline = ''.join(['|' if p.first_sequence[i] == p.second_sequence[i] else 'x' for i in range(0, len(p.first_sequence))])

                    if p.num_mismatches > self.max_num_mismatches or p.num_gaps > 0:
                        # this is the crazy part: read the function docstring for `get_split_palindromes`.
                        # briefly, we conclude that there are too many mismatches in this match, we will
                        # try and see if there is anything we can salvage from it.
                        p_list = self.get_split_palindromes(p)
                    else:
                        # there aren't too many mismatches, and the length checks out. we will continue
                        # processing this hit as a sole palindrome
                        p_list = [p]

                    for sp in p_list:
                        palindromes.append(sp)

        # clean after yourself
        if anvio.DEBUG:
            self.run.info("BLAST temporary dir kept", BLAST_search_tmp_dir, nl_before=1, mc='red')
        else:
            filesnpaths.shutil.rmtree(BLAST_search_tmp_dir)

        return palindromes


    def _find_numba(self, sequence, coords_only=False):
        """Find palindromes in a single sequence using a numba state machine

        This method of palindrome specializes in finding palindromes for short sequences (<5000). If
        the sequence length < 5000, this is a great choice.

        Here are timings as a function of sequence size:

            length 100:   17.6 µs ± 480 ns per loop (mean ± std. dev. of 7 runs, 100000 loops each)
            length 166:   42.8 µs ± 431 ns per loop (mean ± std. dev. of 7 runs, 10000 loops each)
            length 278:   139 µs ± 1.88 µs per loop (mean ± std. dev. of 7 runs, 10000 loops each)
            length 464:   429 µs ± 4.18 µs per loop (mean ± std. dev. of 7 runs, 1000 loops each)
            length 774:   1.3 ms ± 10.5 µs per loop (mean ± std. dev. of 7 runs, 1000 loops each)
            length 1291:  4.01 ms ± 128 µs per loop (mean ± std. dev. of 7 runs, 100 loops each)
            length 2154:  11.2 ms ± 492 µs per loop (mean ± std. dev. of 7 runs, 100 loops each)
            length 3593:  31.3 ms ± 1.05 ms per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 5994:  88.8 ms ± 410 µs per loop (mean ± std. dev. of 7 runs, 10 loops each)
            length 10000: 241 ms ± 1.86 ms per loop (mean ± std. dev. of 7 runs, 1 loop each)

        Parameters
        ==========
        coords_only : bool, False
            If True

        Returns
        =======
        out : list of anvio.sequencefeatures.Palindrome
            By default, returns a list of Palindrome objects. But if coords_only is True, a list of
            4-length tuples are returned. Each tuple is (first_start, first_second, second_start, second_end).
            If no palindromes are found, an empty list is returned.
        """
        sequence_array = utils.nt_seq_to_nt_num_array(sequence)
        sequence_array_RC = utils.nt_seq_to_RC_nt_num_array(sequence)

        palindrome_coords = _find_palindromes(
            seq = sequence_array,
            rev = sequence_array_RC,
            m = self.min_palindrome_length,
            N = self.max_num_mismatches,
            D = self.min_distance,
        )

        # sort by longest palindrome first
        palindrome_coords = sorted(palindrome_coords, key = lambda y: y[1]-y[0], reverse=True)

        if coords_only:
            return palindrome_coords

        palindromes = []
        for first_start, first_end, second_start, second_end in palindrome_coords:
            first_sequence = sequence[first_start:first_end]
            second_sequence = utils.rev_comp(sequence[second_start:second_end])
            num_mismatches = sum(1 for a, b in zip(first_sequence, second_sequence) if a != b)
            midline = ''.join('|' if a == b else 'x' for a, b in zip(first_sequence, second_sequence))

            palindrome = Palindrome(
                first_start = first_start,
                first_end = first_end,
                first_sequence = first_sequence,
                second_start = second_start,
                second_end = second_end,
                second_sequence = second_sequence,
                num_mismatches = num_mismatches,
                midline = midline,
                distance = second_start - first_end,
                length = len(first_sequence),          # length of first will always be length of second
                num_gaps = 0,                          # via the behavior of _find_palindrome
                method = 'numba',
                run = self.run,
            )

            palindromes.append(palindrome)

        return palindromes


    def resolve_mismatch_map(self, s, min_palindrome_length=15, max_num_mismatches=3):
        """Takes a mismatch map, returns longest palindrome start/ends that satisfy all constraints

        If you would like to test this function, a palindrome mismatch map looks
        like this:

            >>> s = 'ooxooooooooooooooooxoooxoxxoxxoxoooooooooxoxxoxooooooooo--ooo-o-xoxoooxoooooooooooooooooo'

        The rest of the code will document itself using the mismatch map `s` as an example :)
        """

        if len(s) < min_palindrome_length:
            return []

        # The purpose here is to find the longest stretches of 'o'
        # characters in a given string of mismatch map `s` (which
        # correspond to matching nts in a palindromic sequence)
        # without violating the maximum number of mismatches allowed
        # by the user (which correspond to `c`) characters. The gaps
        # `-` are not allowed by any means, so they are ignored.
        #
        # A single pass of this algorithm over a `s` to
        # identify sections of it with allowed number of mismathces
        # will not give an opportunity to identify longest possible
        # stretches of palindromic sequences. however, a moving
        # start position WILL consider all combinations of substrings,
        # which can later be considered to find best ones. it is
        # difficult to imagine without an example, so for the string `s`
        # shown above, `starts` will look like this:
        #
        # >>> [0, 3, 20, 24, 26, 27, 29, 30, 32, 42, 44, 45, 47, 65, 67, 71]
        starts = [0] + [pos + 1 for pos, c in enumerate(s) if c == 'x']

        # running the state machine below for a given `start` will collect
        # every matching stretch that contains an acceptable number of
        # mismatches. For instance, for a max mismatch requirement of two,
        # the iterations over `s` will identify start-end positions that
        # will yeild the following substrings:
        #
        # >>>  s: ooxooooooooooooooooxoooxoxxoxxoxoooooooooxoxxoxooooooooo--ooo-o-xoxoooxoooooooooooooooooo
        #
        # >>>  0: ooxooooooooooooooooxooo oxxo xoxooooooooo oxxo ooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>>  3: .. ooooooooooooooooxoooxo xox oxoooooooooxo xoxooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 20: ................... oooxox oxxo oooooooooxox oxooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 24: ....................... oxxo xoxooooooooo oxxo ooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 26: ......................... xox oxoooooooooxo xoxooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 27: .......................... oxxo oooooooooxox oxooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 29: ............................ xoxooooooooo oxxo ooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 30: ............................. oxoooooooooxo xoxooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 32: ............................... oooooooooxox oxooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 42: ......................................... oxxo ooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 44: ........................................... xoxooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 45: ............................................ oxooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 47: .............................................. ooooooooo  ooo o xoxooo oooooooooooooooooo
        # >>> 65: ................................................................ oxoooxoooooooooooooooooo
        # >>> 67: .................................................................. oooxoooooooooooooooooo
        # >>> 71: ...................................................................... oooooooooooooooooo
        #
        # thus, the list `W` will contain start-end positions for every
        # possible stretch that do not include gap characters.
        W = []
        for start in starts:
            end = start
            num_mismatches = 0

            while 1:
                if s[start] == 'x':
                    start += 1
                    continue

                if s[end] == 'o':
                    end += 1
                elif s[end] == 'x':
                    num_mismatches += 1

                    if num_mismatches > max_num_mismatches:
                        W.append((start, end), )
                        num_mismatches = 0
                        start = end + 1
                        end = start
                    else:
                        end += 1
                else:
                    W.append((start, end), )
                    num_mismatches = 0
                    start = end + 1
                    end = start

                if end + 1 > len(s):
                    if end > start:
                        W.append((start, end), )

                    break

        # remove all the short ones:
        W = [(start, end) for (start, end) in W if end - start > min_palindrome_length]

        # sort all based on substring length
        W = sorted(W, key=lambda x: x[1] - x[0], reverse=True)

        # the following code will pop the longest substring from W[0], then
        # remove all the ones that overlap with it, and take the longest one
        # among those that remain, until all items in `W` are considered.
        F = []
        while 1:
            if not len(W):
                break

            _start, _end = W.pop(0)
            F.append((_start, _end), )

            W = [(start, end) for (start, end) in W if (start < _start and end < _start) or (start > _end and end > _end)]

        # now `F` contains the longest substrings with maximum number of
        # mismatches allowed, which will look like this for the `s` with
        # various minimum length (ML) and max mismatches (MM) parameters:
        #
        # >>>    input s: ooxooooooooooooooooxoooxoxxoxxoxoooooooooxoxxoxooooooooo--ooo-o-xoxoooxoooooooooooooooooo
        #
        # >>> ML 5; MM 0:    oooooooooooooooo             ooooooooo      ooooooooo               oooooooooooooooooo
        # >>> ML 5; MM 1:    ooooooooooooooooxooo         oooooooooxo  oxooooooooo           oooxoooooooooooooooooo
        # >>> ML 5; MM 2: ooxooooooooooooooooxooo       oxoooooooooxo xoxooooooooo         oxoooxoooooooooooooooooo
        # >>> ML15; MM 3: ooxooooooooooooooooxoooxo                                        oxoooxoooooooooooooooooo
        # >>> (...)

        return(sorted(F))


    def get_split_palindromes(self, p, display_palindromes=False):
        """Takes a palindrome object, and splits it into multiple.

        The goal here is to make use of BLAST matches that may include too many mismatches or
        gaps, and find long palindromic regions that still fit our criteria of maximum number
        of mismatches and minimum length.

        We go through the mismatch map, resolve regions that still could be good candidates
        to be considered as palindromes, and return a list of curated palindrome objects.
        """

        split_palindromes = []
        mismatch_map = []

        for i in range(0, len(p.first_sequence)):
            if p.first_sequence[i] == p.second_sequence[i]:
                mismatch_map.append('o')
            elif '-' in [p.first_sequence[i], p.second_sequence[i]]:
                mismatch_map.append('-')
            else:
                mismatch_map.append('x')

        mismatch_map = ''.join(mismatch_map)

        # get all the best substrings by resolving the mismatch map
        substrings = self.resolve_mismatch_map(mismatch_map,
                                               min_palindrome_length=self.min_palindrome_length,
                                               max_num_mismatches=self.max_num_mismatches)
        # if we don't get any substrings, it means it is time to go back
        if not len(substrings):
            return []

        if anvio.DEBUG or display_palindromes or self.verbose:
            self.progress.reset()
            self.run.warning(None, header='SPLITTING A HIT', lc='red')
            self.run.info('1st sequence', p.first_sequence, mc='green')
            self.run.info('ALN', p.midline, mc='green')
            self.run.info('2nd sequence', p.second_sequence, mc='green')

        # using these substrings we will generate a list of `Palindrome` objects
        # to replace the mother object.
        for start, end in substrings:
            split_p = Palindrome()
            split_p.sequence_name = p.sequence_name
            split_p.first_start = p.first_start + start
            split_p.first_end = p.first_start + end
            split_p.first_sequence = p.first_sequence[start:end]
            split_p.second_start = p.second_end - end
            split_p.second_end = p.second_end - start
            split_p.second_sequence = p.second_sequence[start:end]
            split_p.midline = p.midline[start:end]
            split_p.num_gaps = split_p.midline.count('-')
            split_p.num_mismatches = split_p.midline.count('x')
            split_p.length = end - start
            split_p.distance = p.distance

            split_palindromes.append(split_p)

            if anvio.DEBUG or display_palindromes or self.verbose:
                self.progress.reset()
                self.run.info_single(f"    Split [{start}:{end}]", nl_before=1, level=2, mc="red")
                self.run.info('    1st sequence', split_p.first_sequence, mc='green')
                self.run.info('    ALN', split_p.midline, mc='green')
                self.run.info('    2nd sequence', split_p.second_sequence, mc='green')

        return split_palindromes


    def report(self):
        num_sequences = 0
        num_palindromes = 0
        longest_palindrome = 0
        most_distant_palindrome = 0

        for sequence_name in self.palindromes:
            num_sequences += 1
            for palindrome in self.palindromes[sequence_name]:
                if palindrome.length > longest_palindrome:
                    longest_palindrome = palindrome.length
                if palindrome.distance > most_distant_palindrome:
                    most_distant_palindrome = palindrome.distance
                num_palindromes += 1

        if num_palindromes == 0:
            self.run.warning(f"Anvi'o searched {P('sequence', num_sequences)} you have provided and found no "
                             f"palindromes that satisfy your input criteria :/ No output file will be generated.")

            return

        self.run.warning(None, header="SEARCH RESULTS", lc="green")
        self.run.info('Total number of sequences processed', num_sequences)
        self.run.info('Total number of palindromes found', num_palindromes)
        self.run.info('Longest palindrome', longest_palindrome)
        self.run.info('Most distant palindrome', most_distant_palindrome)

        headers = ["sequence_name", "length", "distance", "num_mismatches", "first_start", "first_end", "first_sequence", "second_start", "second_end", "second_sequence", "midline"]
        if self.output_file_path:
            with open(self.output_file_path, 'w') as output_file:
                output_file.write('\t'.join(headers) + '\n')
                for sequence_name in self.palindromes:
                    for palindrome in self.palindromes[sequence_name]:
                        output_file.write('\t'.join([f"{getattr(palindrome, h)}" for h in headers]) + '\n')

            self.run.info('Output file', self.output_file_path, mc='green', nl_before=1, nl_after=1)


@jit(nopython=True)
def _find_palindromes(seq, rev, m, N, D):
    L = len(seq)
    palindrome_coords = []

    i = 0
    while i < L-m+1:
        j = 0
        skip_i_amount = 0
        while j < L-i-m:
            skip_j_amount = 0
            if rev[j] != seq[i]:
                # The (i, j) scenario doesn't even _start_ with a match. Game over.
                pass
            else:
                n, k = 0, 0
                last_match = 0
                is_palindrome = False
                while True:
                    if (i+k+1) > (L-j-k-1):
                        # Stop of left has exceeded start of right.
                        if is_palindrome:
                            palindrome_coords.append((i, i+last_match+1, L-j-last_match-1, L-j))
                            skip_j_amount = last_match
                            skip_i_amount = last_match if last_match > skip_i_amount else skip_i_amount
                        break

                    if rev[j+k] == seq[i+k]:
                        last_match = k
                    else:
                        # mismatch
                        n += 1

                    if n > N:
                        if is_palindrome:
                            palindrome_coords.append((i, i+last_match+1, L-j-last_match-1, L-j))
                            skip_j_amount = last_match
                            skip_i_amount = last_match if last_match > skip_i_amount else skip_i_amount
                        break

                    if last_match == m-1:
                        is_palindrome = True

                    if (L-j-k-1) - (i+k+1) < D:
                        break

                    k += 1
            j += skip_j_amount + 1
        i += skip_i_amount + 1

    return palindrome_coords


def _find_palindromes_test(seq, rev, m, N, D):
    L = len(seq)
    palindrome_coords = []

    i = 0
    while i < L-m+1:
        j = 0
        skip_i_amount = 0
        while j < L-i-m:
            skip_j_amount = 0
            if rev[j] != seq[i]:
                get_state(seq, i, j, 0)
                print('No match between i and j')
                # The (i, j) scenario doesn't even _start_ with a match. Game over.
                pass
            else:
                n, k = 0, 0
                last_match = 0
                is_palindrome = False
                while True:
                    get_state(seq, i, j, k)
                    if (i+k+1) > (L-j-k-1):
                        print('Stop of left has exceeded start of right.')
                        # Stop of left has exceeded start of right.
                        if is_palindrome:
                            palindrome_coords.append((i, i+last_match+1, L-j-last_match-1, L-j))
                            skip_j_amount = last_match
                            skip_i_amount = last_match if last_match > skip_i_amount else skip_i_amount
                            print(f'Palindrome {(i, i+last_match+1, L-j-last_match-1, L-j)}.')
                        break

                    if rev[j+k] == seq[i+k]:
                        last_match = k
                    else:
                        # mismatch
                        n += 1

                    if n > N:
                        print('Max # mismatches met. Calling quits')
                        if is_palindrome:
                            palindrome_coords.append((i, i+last_match+1, L-j-last_match-1, L-j))
                            skip_j_amount = last_match
                            skip_i_amount = last_match if last_match > skip_i_amount else skip_i_amount
                            print(f'Palindrome {(i, i+last_match+1, L-j-last_match-1, L-j)}.')
                        break

                    if last_match == m-1:
                        print('Min palindrome length met')
                        is_palindrome = True

                    if (L-j-k-1) - (i+k+1) < D:
                        print('Min gap distance not satisfied')
                        break

                    k += 1
            print(f'skipping j ahead {skip_j_amount}')
            j += skip_j_amount + 1

        print(f'skipping i ahead {skip_i_amount}')
        i += skip_i_amount + 1

    return palindrome_coords


def get_state(seq, i, j, k):
    print()
    print(''.join([str(s) for s in seq]))
    state = [' ']*len(seq)
    state[i] = 'i'
    state[len(seq)-1-j] = 'j'
    state[k+i] = 'k'
    print(''.join(state))



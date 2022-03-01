import os
import pathlib
import tempfile
import unittest

# from ..parse import ProvDAG
from ..replay import replay_fp
from .test_parse import DATA_DIR


class ReplayPythonUsageTests(unittest.TestCase):
    def test_template_action_lumps_many_outputs(self):
        """
        ReplayPythonUsage._template_action should "lump" multiple outputs from
        one command into a single Results-like object when the total number of
        outputs from a single command > 5

        In these cases, our rendering should look like:
        `action_results = plugin_actions.action()...`
        instead of:
        `_, _, thing3, _, _, _ = plugin_actions.action()...`

        In this artifact, we are only replaying one results from core-metrics,
        but because core_metrics has a million results it should stil lump em.
        """
        in_fp = os.path.join(DATA_DIR, 'v5_uu_emperor.qzv')
        driver = 'python3'
        exp = ('(?s)action_results = diversity_actions.core_metrics_phylo.*'
               'unweighted_unifrac_emperor.*action_results.unweighted_unifrac')
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = pathlib.Path(tmpdir) / 'action_collection.txt'
            replay_fp(in_fp, out_path, driver)

            with open(out_path, 'r') as fp:
                rendered = fp.read()
        self.assertRegex(rendered, exp)

    def test_template_action_does_not_lump_four_outputs(self):
        """
        ReplayPythonUsage._template_action should not "lump" multiple outputs
        one command into a single Results-like object when the total number of
        outputs from a single command <= 5, unless the total number of results
        is high (see above).

        In these cases, our rendering should look like:
        `_, _, thing3, _ = plugin_actions.action()...`
        instead of:
        `action_results = plugin_actions.action()...`

        In this case, we are replaying one result from an action which has four
        results. It should not lump em.
        """
        in_fp = os.path.join(DATA_DIR, 'v5_uu_emperor.qzv')
        driver = 'python3'
        exp = ('(?s)_, _, _, rooted_tree_0 = phylogeny_actions.align_to_tre.*')
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = pathlib.Path(tmpdir) / 'action_collection.txt'
            replay_fp(in_fp, out_path, driver)

            with open(out_path, 'r') as fp:
                rendered = fp.read()
        self.assertRegex(rendered, exp)

    def test_template_action_lumps_three_variables(self):
        """
        ReplayPythonUsage._template_action should "lump" multiple outputs from
        one command into a single Results-like object when there are more than
        two usage variables (i.e. replay of 3+ results from a single command)

        In these cases, our rendering should look like:
        ```
        action_results = plugin_actions.action(...)
        thing1 = action_results.thinga
        etc.
        ```
        instead of:
        `thing1, _, thing3, _, thing5, _ = plugin_actions.action()...`

        In this test, we are replaying three results from dada2.denoise_single,
        which should be lumped.
        """
        in_fp = os.path.join(DATA_DIR, 'lump_three_vars_test')
        driver = 'python3'
        exp = ('(?s)action_results = dada2_actions.denoise_single.*'
               'representative_sequences_0 = action_results.representative_s.*'
               'denoising_stats_0 = action_results.denoising_stats.*'
               'table_0 = action_results.table.*'
               )
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = pathlib.Path(tmpdir) / 'action_collection.txt'
            replay_fp(in_fp, out_path, driver)

            with open(out_path, 'r') as fp:
                rendered = fp.read()
        self.assertRegex(rendered, exp)

    def test_template_action_does_not_lump_two_vars(self):
        """
        ReplayPythonUsage._template_action should not "lump" multiple outputs
        from one command into a single Results-like object when the total count
        of usage variables (i.e. replayed outputs) from a single command < 3,
        unless the total number of outputs is high (see above).

        In these cases, our rendering should look like:
        `thing1, _, thing3, _ = plugin_actions.action()...`
        instead of:
        `action_results = plugin_actions.action()...`

        In this case, we are replaying two results from dada2.denoise_single,
        which should not be lumped.
        """
        in_fp = os.path.join(DATA_DIR, 'v5_uu_emperor.qzv')
        driver = 'python3'
        exp = ('(?s)table_0, representative_sequences_0, _ = dada2_actions.*')
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = pathlib.Path(tmpdir) / 'action_collection.txt'
            replay_fp(in_fp, out_path, driver)

            with open(out_path, 'r') as fp:
                rendered = fp.read()
        self.assertRegex(rendered, exp)

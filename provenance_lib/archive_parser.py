import abc
from dataclasses import dataclass
from io import BytesIO
import networkx as nx
import os
import pandas as pd
import pathlib
from datetime import timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml
import warnings
import zipfile

import bibtexparser as bp

from . import checksum_validator
from . import version_parser
from .util import get_root_uuid, get_nonroot_uuid, UUID, FileName
from .yaml_constructors import CONSTRUCTOR_REGISTRY, MetadataInfo

for key in CONSTRUCTOR_REGISTRY:
    yaml.SafeLoader.add_constructor(key, CONSTRUCTOR_REGISTRY[key])


@dataclass(frozen=False)
class Config():
    perform_checksum_validation: bool = True
    parse_study_metadata: bool = True
    verbose: bool = False


@dataclass
class ParserResults():
    """
    Results generated by a ParserVx
    """
    parsed_artifact_uuids: Set[UUID]
    prov_digraph: nx.DiGraph
    provenance_is_valid: checksum_validator.ValidationCode
    checksum_diff: Optional[checksum_validator.ChecksumDiff]


class ProvNode:
    """ One node of a provenance DAG, describing one QIIME 2 Result """

    @property
    def _uuid(self) -> UUID:
        return self._result_md.uuid

    @_uuid.setter
    def _uuid(self, new_uuid: UUID):
        """
        ProvNode's UUID. Safe for use as getter, but prefer
        ProvDAG.relabel_nodes to using this property as a setter.
        That method preserves alignment between ids across the dag
        and its ProvNodes.
        """
        self._result_md.uuid = new_uuid

    @property
    def type(self) -> str:
        return self._result_md.type

    @property
    def format(self) -> Optional[str]:
        return self._result_md.format

    @property
    def archive_version(self) -> str:
        return self._archive_version

    @property
    def framework_version(self) -> str:
        return self._framework_version

    @property
    def has_provenance(self) -> bool:
        return self.archive_version != '0'

    @property
    def citations(self) -> Dict:
        citations = {}
        if hasattr(self, '_citations'):
            citations = self._citations.citations
        return citations

    @property
    def metadata(self) -> Optional[Dict[str, pd.DataFrame]]:
        """
        A dict containing {parameter_name: metadata_dataframe} pairs, where
        parameter_name is the registered name of the parameter the Metadata
        or MetadataColumn was passed to.

        Returns {} if this action took in no Metadata or MetadataColumn

        Returns None if this action has no metadata because the archive has no
        provenance, or the user opted out of metadata parsing.
        """
        self._metadata: Optional[Dict[str, pd.DataFrame]]

        md = None
        if hasattr(self, '_metadata'):
            md = self._metadata
        return md

    @property
    def _parents(self) -> Optional[List[Dict[str, UUID]]]:
        """
        a list of single-item {Type: UUID} dicts describing this
        action's inputs, and including Artifacts passed as Metadata parameters.

        Returns [] if this "action" is an Import

        # TODO: I think this is no longer true, so long as the user
        # sticks with the provided relabel method. Are there otheres?
        NOTE: This property is "private" because it is unsafe,
        reporting original node IDs that are not updated if the user renames
        nodes using the ProvDAG/networkx API (e.g. nx.relabel_nodes).
        ProvDAG and its extensions should use the networkx.DiGraph itself to
        work with ancestry.
        """
        self._artifacts_passed_as_md: List[Dict[str, UUID]]

        if not self.has_provenance:
            return None

        inputs = self.action._action_details.get('inputs')
        parents = []
        if inputs is not None:
            # Inputs are a list of single-item dicts, so we have to
            for input in inputs:
                name, value = next(iter(input.items()))
                # value is usually a uuid, but may be a collection of uuids.
                # the following are specced in qiime2/core/type/collection
                if type(value) in (set, list, tuple):
                    for i in range(len(value)):
                        # Make these unique in case the single-item dicts get
                        # merged into a single dict downstream.
                        unq_name = f'{name}_{i}'
                        parents.append({unq_name: value[i]})
                elif value is not None:
                    parents.append({name: value})
                else:
                    # skip None-by-default optional inputs
                    # covered by test_parents_for_table_with_optional_input
                    pass  # pragma: no cover
        return parents + self._artifacts_passed_as_md

    def __init__(self, cfg: Config, zf: zipfile.ZipFile,
                 fps_for_this_result: List[pathlib.Path]) -> None:
        """
        Constructs a ProvNode from a zipfile and some filepaths.

        This constructor is intentionally flexible, and will parse any
        files handed to it. It is the responsibility of the ParserVx classes to
        decide what files need to be passed.
        """
        for fp in fps_for_this_result:
            if fp.name == 'VERSION':
                self._archive_version, self._framework_version = \
                    version_parser.parse_version(zf, fp)
            elif fp.name == 'metadata.yaml':
                self._result_md = _ResultMetadata(zf, str(fp))
            elif fp.name == 'action.yaml':
                self.action = _Action(zf, str(fp))
            elif fp.name == 'citations.bib':
                self._citations = _Citations(zf, str(fp))
            elif fp.name == 'checksums.md5':
                # Handled in ProvDAG
                pass

        if self.has_provenance:
            all_metadata_fps, self._artifacts_passed_as_md = \
                self._get_metadata_from_Action(self.action._action_details)
            if cfg.parse_study_metadata:
                self._metadata = self._parse_metadata(zf, all_metadata_fps)

    def _get_metadata_from_Action(
        self, action_details: Dict[str, List]) \
            -> Tuple[Dict[str, str], List[Dict[str, UUID]]]:
        """
        Gathers data related to Metadata and MetadataColumn-based metadata
        files from an in-memory representation of an action.yaml file.

        Specifically:

        - it captures filepath and parameter-name data for _all_
        metadata files, so that these can be located for parsing, and then
        associated with the correct parameters during replay.

        - it captures uuids for all artifacts passed to this action as
        metadata so they can be included as parents of this node.

        Returns a two-tuple (all_metadata, artifacts_as_metadata) where:
        - all-metadata conforms to {parameter_name: relative_filename}
        - artifacts_as_metadata is a list of single-item dictionaries
        conforming to [{'artifact_passed_as_metadata': <uuid>}, ...]

        Input data looks like this:

        {'action': {'parameters': [{'some_param': 'foo'},
                                   {'arbitrary_metadata_name':
                                    {'input_artifact_uuids': [],
                                     'relative_fp': 'sample_metadata.tsv'}},
                                   {'other_metadata':
                                    {'input_artifact_uuids': ['4154...301b4'],
                                     'relative_fp': 'feature_metadata.tsv'}},
                                   ]
                    }}

        as loaded from this YAML:

        action:
            parameters:
            -   some_param: 'foo'
            -   arbitrary_metadata_name: !metadata 'sample_metadata.tsv'
            -   other_metadata: !metadata '4154...301b4:feature_metadata.tsv'

        NOTE: When Artifacts are passed as Metadata, they are captured in
        action.py's action['parameters'], rather than in action['inputs'] with
        the other Artifacts. As a result, Semantic Type data is not captured.
        This function returns a hardcoded filler 'Type' for all UUIDs
        discovered here: 'artifact_passed_as_metadata'. This will not match the
        actual Type of the parent Artifact, but the properties of provenance
        DiGraphs make this irrelevant. Because Artifacts passed as Metadata
        retain their provenance, downstream Artifacts are linked to their
        "real" parent Artifact nodes, which have accurate Type information.
        The filler type is moot.
        """
        all_metadata = dict()
        artifacts_as_metadata = []
        if (all_params := action_details.get('parameters')) is not None:
            for param in all_params:
                param_val = next(iter(param.values()))
                if isinstance(param_val, MetadataInfo):
                    param_name = next(iter(param))
                    md_fp = param_val.relative_fp
                    all_metadata.update({param_name: md_fp})

                    artifacts_as_metadata += [
                        {'artifact_passed_as_metadata': uuid} for uuid in
                        param_val.input_artifact_uuids]

        return all_metadata, artifacts_as_metadata

    def _parse_metadata(self, zf: zipfile.ZipFile,
                        metadata_fps: Dict[str, str]) -> \
            Dict[str, pd.DataFrame]:
        """
        Parses all metadata files captured from Metadata and MetadataColumns
        (identifiable by !metadata tags) into pd.DataFrames.

        Returns an empty dict if there is no metadata.

        In the future, we may need a simple type that can hold the name of the
        original associated parameter, the type (MetadataColumn or Metadata),
        and the appropriate Series or Dataframe respectively.
        """
        root_uuid = get_root_uuid(zf)
        pfx = pathlib.Path(root_uuid) / 'provenance'
        if root_uuid == self._uuid:
            pfx = pfx / 'action'
        else:
            pfx = pfx / 'artifacts' / self._uuid / 'action'

        all_md = dict()
        for param_name in metadata_fps:
            filename = str(pfx / metadata_fps[param_name])
            with zf.open(filename) as myfile:
                df = pd.read_csv(BytesIO(myfile.read()), sep='\t')
                all_md.update({param_name: df})

        return all_md

    def __repr__(self) -> str:
        return repr(self._result_md)

    __str__ = __repr__

    def __hash__(self) -> int:
        return hash(self._uuid)

    def __eq__(self, other) -> bool:
        return (self.__class__ == other.__class__
                and self._uuid == other._uuid
                )


class _Action:
    """ Provenance data from action.yaml for a single QIIME 2 Result """

    @property
    def action_id(self) -> str:
        """ the UUID assigned to this Action (not its Results) """
        return self._execution_details['uuid']

    @property
    def action_type(self) -> str:
        """
        The type of Action represented (e.g. Method, Pipeline, etc. )
        Returns Import if an import - this is a useful sentinel for deciding
        what type of action we're parsing (Action vs import)
        """
        return self._action_details['type']

    @property
    def runtime(self) -> timedelta:
        """
        The elapsed run time of the Action, as a datetime object
        """
        end = self._execution_details['runtime']['end']
        start = self._execution_details['runtime']['start']
        return end - start

    @property
    def runtime_str(self) -> str:
        """
        The elapsed run time of the Action, in Seconds and microseconds
        """
        return self._execution_details['runtime']['duration']

    @property
    def action_name(self) -> str:
        """
        The name of the action itself. Returns 'import' if this is an import.
        """
        action_name = self._action_details.get('action')
        if self.action_type == 'import':
            action_name = 'import'
        return action_name

    @property
    def plugin(self) -> str:
        """
        The plugin which executed this Action. Returns 'framework' if this is
        an import.
        """
        plugin = self._action_details.get('plugin')
        if self.action_type == 'import':
            plugin = 'framework'
        return plugin

    @property
    def inputs(self) -> dict:
        """ returns a dict of artifact inputs to this action """
        inputs = self._action_details.get('inputs')
        results = {}
        if inputs is not None:
            for item in inputs:
                results.update(item.items())
        return results

    @property
    def parameters(self) -> dict:
        """ returns a dict of parameters passed to this action """
        params = self._action_details.get('parameters')
        results = {}
        if params is not None:
            for item in params:
                results.update(item.items())
        return results

    @property
    def output_name(self) -> Optional[str]:
        """
        Returns the output name for the node that owns this action.yaml
        note that a QIIME 2 action may have multiple outputs not represented
        here.
        """
        return self._action_details.get('output-name')

    @property
    def format(self) -> Optional[str]:
        """
        Returns this action's format field if any.
        Expected with actions of type import, maybe no others?
        """
        return self._action_details.get('format')

    @property
    def transformers(self) -> Optional[Dict]:
        """
        Returns this action's transformers dictionary if any.
        """
        return self._action_dict.get('transformers')

    def __init__(self, zf: zipfile.ZipFile, fp: str):
        self._action_dict = yaml.safe_load(zf.read(fp))
        self._action_details = self._action_dict['action']
        self._execution_details = self._action_dict['execution']
        self._env_details = self._action_dict['environment']

    def __repr__(self):
        return (f"_Action(action_id={self.action_id}, type={self.action_type},"
                f" plugin={self.plugin}, action={self.action_name})")


class _Citations:
    """
    citations for a single QIIME 2 Result, as a dict of citation dicts keyed
    on the citation's bibtex ID.

    This ID is also stored in the value dicts, making it straightforward to
    convert these back to BibDatabase objects e.g. list(self.citations.values()
    """
    def __init__(self, zf: zipfile.ZipFile, fp: str):
        bib_db = bp.loads(zf.read(fp))
        self.citations = bib_db.get_entry_dict()

    def __repr__(self):
        keys = list(self.citations.keys())
        return (f"Citations({keys})")


class _ResultMetadata:
    """ Basic metadata about a single QIIME 2 Result from metadata.yaml """
    def __init__(self, zf: zipfile.ZipFile, md_fp: str):
        _md_dict = yaml.safe_load(zf.read(md_fp))
        self.uuid = _md_dict['uuid']
        self.type = _md_dict['type']
        self.format = _md_dict['format']

    def __repr__(self):
        return (f"UUID:\t\t{self.uuid}\n"
                f"Type:\t\t{self.type}\n"
                f"Data Format:\t{self.format}")


class Parser(metaclass=abc.ABCMeta):
    accepted_data_types: str

    @classmethod
    @abc.abstractmethod
    def get_parser(cls, artifact_data: Any) -> 'Parser':
        """
        Return the appropriate Parser if this Parser type can handle the data
        passed in.

        Should raise an appropriate exception if this Parser cannot handle the
        data.
        """

    @abc.abstractmethod
    def parse_prov(self, cfg: Config, data: Any) -> ParserResults:
        """
        Parse provenance returning a ParserResults
        """


class ArtifactParser(Parser):
    # description from (and more details available at)
    # https://docs.python.org/3/library/zipfile.html#zipfile-objects
    accepted_data_types = ("a path to a file (a string) or a file-like object")

    @classmethod
    def get_parser(cls, artifact_data: Any) -> Parser:
        """
        Returns the correct archive format parser for a zip archive.
        """
        try:
            is_dir = os.path.isdir(artifact_data)
        except TypeError:
            is_dir = False

        if is_dir:
            raise ValueError("ArtifactParser expects a file, not a directory")

        try:
            # By trying to open artifact_data directly, we get more
            # informative errors than with `if zipfile.is_zipfile():`
            with zipfile.ZipFile(artifact_data, 'r') as zf:
                archive_version, _ = \
                    version_parser.parse_version(zf)
            return FORMAT_REGISTRY[archive_version]()
        except Exception as e:
            # Re-raise after appending the name of this parser to the error
            # message, so we can figure out which parser it's coming from
            raise type(e)(f" in ArtifactParser: {str(e)}")

    def parse_prov(cls, cfg: Config, data: Any) -> ParserResults:
        raise NotImplementedError(
            "Use a subclass that usefully defines parse_prov for some format."
        )


class ParserV0(ArtifactParser):
    """
    Parser for V0 archives. These have no provenance, so we only parse metadata
    """
    # These are files we expect will be present in every QIIME2 archive with
    # this format. "Optional" filenames (like Metadata, which may or may
    # not be present in an archive) should not be included here.
    expected_files_root_only = tuple()  # type: Tuple[str, ...]
    expected_files_in_all_nodes = (
        'metadata.yaml', 'VERSION')  # type: Tuple[str, ...]

    def parse_prov(self, cfg: Config, archive_data: FileName) -> ParserResults:
        archv_contents = {}

        with zipfile.ZipFile(archive_data) as zf:
            if cfg.perform_checksum_validation:
                provenance_is_valid, checksum_diff = \
                    self._validate_checksums(zf)
            else:
                provenance_is_valid, checksum_diff = (
                    checksum_validator.ValidationCode.VALIDATION_OPTOUT, None)

            uuid = get_root_uuid(zf)

            warnings.warn(f"Artifact {uuid} was created prior to provenance" +
                          " tracking. Provenance data will be incomplete.",
                          UserWarning)

            root_md = self._parse_root_md(zf, uuid)
            parsed_artifact_uuids = {root_md.uuid}
            expected_files = self.expected_files_in_all_nodes
            prov_data_fps = [pathlib.Path(uuid) / fp for fp in expected_files]
            archv_contents[uuid] = ProvNode(cfg, zf, prov_data_fps)
            archv_contents = self._digraph_from_archive_contents(
                archv_contents)

        return ParserResults(
            parsed_artifact_uuids,
            archv_contents,
            provenance_is_valid,
            checksum_diff,
            )

    def _parse_root_md(self, zf: zipfile.ZipFile, root_uuid: UUID) \
            -> _ResultMetadata:
        """ Get archive metadata including root uuid """
        # All files in zf start with root uuid, so we'll grab it from the first
        root_md_fp = root_uuid + '/metadata.yaml'
        if root_md_fp not in zf.namelist():
            raise ValueError("Malformed Archive: root metadata.yaml file "
                             f"misplaced or nonexistent in {zf.filename}")
        return _ResultMetadata(zf, root_md_fp)

    def _validate_checksums(self, zf: zipfile.ZipFile) -> \
            Tuple[checksum_validator.ValidationCode,
                  Optional[checksum_validator.ChecksumDiff]]:
        """
        V0 archives predate provenance tracking, so
        - provenance_is_valid = False
        - checksum_diff = None
        """
        return (checksum_validator.ValidationCode.PREDATES_CHECKSUMS,
                None)

    def _digraph_from_archive_contents(
            self, archive_contents: Dict[UUID, 'ProvNode']) -> nx.DiGraph:
        """
        Builds a networkx.DiGraph from a {UUID: ProvNode} dictionary, like the
        one created in parse_prov().

        0. Create an empty nx.digraph
        1. gather nodes and their required attributes in an n_bunch and add
           to the DiGraph
        2. Add edges to graph (including all !no-provenance nodes)
        3. Create guaranteed node attributes for these no-provenance nodes,
           which wouldn't otherwise have them.
        """
        dag = nx.DiGraph()
        nbunch = [
            (n_id, dict(
                node_data=archive_contents[n_id],
                has_provenance=archive_contents[n_id].has_provenance,
                )) for n_id in archive_contents]
        dag.add_nodes_from(nbunch)

        ebunch = []
        for node_id, attrs in dag.nodes(data=True):
            if parents := attrs['node_data']._parents:
                for parent in parents:
                    # parent is a single-item {type: uuid} dict
                    parent_uuid = next(iter(parent.values()))
                    ebunch.append((parent_uuid, node_id))
        dag.add_edges_from(ebunch)

        for node_id, attrs in dag.nodes(data=True):
            if attrs.get('node_data') is None:
                attrs['has_provenance'] = False
                attrs['node_data'] = None

        return dag


class ParserV1(ParserV0):
    """
    Parser for V1 archives. These track provenance, so we parse it.
    """
    version_string = 1
    # These are files we expect will be present in every QIIME2 archive with
    # this format. "Optional" filenames should not be included here.
    expected_files_root_only = ParserV0.expected_files_root_only
    expected_files_in_all_nodes = ParserV0.expected_files_in_all_nodes + \
        ('action/action.yaml', )

    def parse_prov(self, cfg: Config, archive_data: FileName) -> ParserResults:
        """
        Parses provenance data for one Archive.

        By convention, the filepaths within these Archives begin with:
        <archive_root_uuid>/provenance/...
        archive-root provenance files live directly inside 'provenance'
        e.g: <archive_root_uuid>/provenance/metadata.yaml
        non-root provenance files live inside 'artifacts/<uuid>'
        e.g: <archive_root_uuid>/provenance/artifacts/<uuid>/metadata.yaml
        or <archive_root_uuid>/provenance/artifacts/<uuid>/action/action.yaml
        """
        archv_contents = {}

        with zipfile.ZipFile(archive_data) as zf:
            if cfg.perform_checksum_validation:
                provenance_is_valid, checksum_diff = \
                    self._validate_checksums(zf)
            else:
                provenance_is_valid, checksum_diff = (
                    checksum_validator.ValidationCode.VALIDATION_OPTOUT, None)

            prov_data_fps = self._get_prov_data_fps(
                zf, self.expected_files_in_all_nodes,
                self.expected_files_root_only)
            root_uuid = get_root_uuid(zf)

            root_md = self._parse_root_md(zf, root_uuid)

            # make a provnode for each UUID
            for fp in prov_data_fps:
                fps_for_this_result = []
                # if no 'artifacts' -> this is provenance for the archive root
                if 'artifacts' not in fp.parts:
                    node_uuid = root_uuid
                    prefix = pathlib.Path(node_uuid) / 'provenance'
                    root_only_expected_fps = [
                        pathlib.Path(node_uuid) / filename for filename in
                        self.expected_files_root_only]
                    fps_for_this_result += root_only_expected_fps
                else:
                    node_uuid = get_nonroot_uuid(fp)
                    prefix = pathlib.Path(*fp.parts[0:4])

                if node_uuid not in archv_contents:
                    # get version-specific expected_files_in_all_nodes
                    v_fp = prefix / 'VERSION'
                    result_vzn, _ = version_parser.parse_version(zf, v_fp)
                    exp_files = \
                        FORMAT_REGISTRY[result_vzn].expected_files_in_all_nodes

                    fps_for_this_result.extend(
                        [prefix / name for name in exp_files])

                    # Warn and reset provenance_is_valid if expected files are
                    # missing
                    files_are_missing = False
                    error_contents = "Malformed Archive: "
                    for fp in fps_for_this_result:
                        if fp not in prov_data_fps:
                            files_are_missing = True
                            provenance_is_valid = \
                                checksum_validator.ValidationCode.INVALID
                            error_contents += (
                                f"{fp.name} file for node {node_uuid} "
                                f"misplaced or nonexistent in {zf.filename}.\n"
                                )

                    if(files_are_missing):
                        error_contents += (
                            f"Archive {root_uuid} may be corrupt "
                            "or provenance may be false.")
                        raise ValueError(error_contents)

                    archv_contents[node_uuid] = ProvNode(cfg, zf,
                                                         fps_for_this_result)

        archv_contents = self._digraph_from_archive_contents(archv_contents)

        parsed_artifact_uuids = {root_md.uuid}
        return ParserResults(
            parsed_artifact_uuids,
            archv_contents,
            provenance_is_valid,
            checksum_diff
        )

    def _validate_checksums(self, zf: zipfile.ZipFile) -> \
            Tuple[checksum_validator.ValidationCode,
                  Optional[checksum_validator.ChecksumDiff]]:
        """
        Provenance is initially assumed valid because we have no checksums,
        so:
        - provenance_is_valid = False
        - checksum_diff = None
        """
        return (checksum_validator.ValidationCode.PREDATES_CHECKSUMS,
                None)

    def _get_prov_data_fps(self, zf: zipfile.ZipFile,
                           expected_files_all_nodes: Tuple['str', ...],
                           expected_files_root_only: Tuple['str', ...]) -> \
            List[pathlib.Path]:
        fps = [pathlib.Path(fp) for fp in zf.namelist()
               if 'provenance' in fp
               # and any of the expected filenames show up in the filepath
               and any(map(lambda x: x in fp, expected_files_all_nodes))
               ]
        # some files (checksums.md5) exist only at the root level, so we add em
        root_uuid = get_root_uuid(zf)
        fps.extend(
            [pathlib.Path(root_uuid) / filename
             for filename in expected_files_root_only])
        return fps


class ParserV2(ParserV1):
    """
    Parser for V2 archives. Directory structure identical to V1
    action.yaml changes to support Pipelines
    """
    version_string = 2
    # These are files we expect will be present in every QIIME2 archive with
    # this format. "Optional" filenames should not be included here.
    expected_files_in_all_nodes = ParserV1.expected_files_in_all_nodes
    expected_files_root_only = ParserV1.expected_files_root_only


class ParserV3(ParserV2):
    """
    Parser for V3 archives. Directory structure identical to V1 & V2
    action.yaml now supports variadic inputs, so !set tags in action.yaml
    """
    version_string = 3
    # These are files we expect will be present in every QIIME2 archive with
    # this format. "Optional" filenames should not be included here.
    expected_files_in_all_nodes = ParserV2.expected_files_in_all_nodes
    expected_files_root_only = ParserV2.expected_files_root_only


class ParserV4(ParserV3):
    """
    Parser for V4 archives. Adds citations to dir structure, changes to
    action.yaml incl transformers
    """
    version_string = 4
    # These are files we expect will be present in every QIIME2 archive with
    # this format. "Optional" filenames should not be included here.
    expected_files_in_all_nodes = ParserV3.expected_files_in_all_nodes + \
        ('citations.bib', )
    expected_files_root_only = ParserV3.expected_files_root_only


class ParserV5(ParserV4):
    """
    Parser for V5 archives. Adds checksum validation with checksums.md5
    """
    version_string = 5
    # These are files we expect will be present in every QIIME2 archive with
    # this format. "Optional" filenames should not be included here.
    expected_files_in_all_nodes = ParserV4.expected_files_in_all_nodes
    expected_files_root_only = ('checksums.md5', )

    def parse_prov(self, cfg: Config, archive_data: FileName) -> ParserResults:
        """
        Parses provenance data for one Archive, applying the local
        _validate_checksums() method to the v1 parser
        """
        return super().parse_prov(cfg, archive_data)

    def _validate_checksums(self, zf: zipfile.ZipFile) -> \
            Tuple[checksum_validator.ValidationCode,
                  Optional[checksum_validator.ChecksumDiff]]:
        """
        With v5, we can actually validate checksums, so use checksum_validator
        to return:
        - provenance_is_valid: bool
        - checksum_diff: Optional[ChecksumDiff], where None only if
            checksums.md5 is missing
        """
        return checksum_validator.validate_checksums(zf)


FORMAT_REGISTRY = {
    # NOTE: update for new format versions in qiime2.core.archive.Archiver
    '0': ParserV0,
    '1': ParserV1,
    '2': ParserV2,
    '3': ParserV3,
    '4': ParserV4,
    '5': ParserV5,
}

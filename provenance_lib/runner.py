# flake8: noqa
import sys
import zipfile

import yaml

sys.path.append('/home/chris/src/provenance_py')

if __name__ == "__main__" and __package__ is None:
    __package__ = "provenance_lib"

from .parse import ProvDAG, Config
from .replay import replay_provdag

if __name__ == '__main__':
    # To begin, we'll read in exactly one fp
    if len(sys.argv) != 2:
        raise ValueError('Please pass one filepath to a QIIME 2 Archive')

    archive_fp = sys.argv[1]
    dummy_DAG = ProvDAG(archive_fp)

    # We're only parsing one Artifact here, so need to grab the only term. uuid
    terminal_uuid, *_ = dummy_DAG.terminal_uuids
    deets = dummy_DAG.get_node_data(terminal_uuid).action._action_details
    plurg = deets['plugin']
    ackshun = deets['action']

    print(f'{repr(dummy_DAG)}')

    terminal_node = next(iter(dummy_DAG.terminal_nodes))
    print(f'{terminal_node._result_md}')
    print(f'- was made by q2-{plurg} {ackshun}')
    print(f'- contains prov. data from {len(dummy_DAG)}'
          ' QIIME 2 Results, mostly ancestors')
    # print(dummy_DAG._prov_digraph)

    print(f'\nIts prov DAG looks like\n{dummy_DAG}')

    print('#########################################')
    print(
          dummy_DAG
          .get_node_data('ffb7cee3-2f1f-4988-90cc-efd5184ef003')._parents)

    print("\nTopological sort of dummy dag: ")
    replay_provdag(dummy_DAG)

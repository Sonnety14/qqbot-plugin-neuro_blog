import sys,importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'optional'),str(ROOT/'services')]
spec=importlib.util.spec_from_file_location('astrbot_plugin_neuro_blog',ROOT/'__init__.py',submodule_search_locations=[str(ROOT)])
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)

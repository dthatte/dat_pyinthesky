#!/usr/bin/env python

import logging
import glob
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import typing

root = logging.getLogger()
root.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
root.addHandler(handler)

logger = logging.getLogger(__file__)

BUILD_BASE_DIR = '/tmp/nbcollection-ci-build-base-dir'
ARTIFACT_DEST_DIR: str = '/tmp/artifacts'
PWN = typing.TypeVar('PWN')
IPYDB_REQUIRED_FILES: typing.List[str] = ['requirements.txt']
REQUIREMENTS_FILE_NAMES = ['requirements.txt', 'requirements']
ENCODING: str = 'utf-8'
if os.path.exists(ARTIFACT_DEST_DIR):
    shutil.rmtree(ARTIFACT_DEST_DIR)

os.makedirs(ARTIFACT_DEST_DIR)

class Notebook(typing.NamedTuple):
    name: str
    filename: str
    filepath: str

    def inject_notebook(self: PWN, build_dir: str) -> None:
        build_path = os.path.join(build_dir, self.filename)
        shutil.copyfile(self.filepath, build_path)

    def create_build_script(self: PWN, categories: typing.List[str], build_dir: str, artifact_dir: str) -> None:
        build_script_filepath = os.path.join(build_dir, f'{self.filename}-builder.sh')
        categories_formatted = '/'.join(categories)
        metadata_path = f'{artifact_dir}/{categories_formatted}/{self.filename}.metadata.json'
        html_path = f'{artifact_dir}/{categories_formatted}/{self.filename}.html'
        output_dir = os.path.dirname(html_path)
        build_script = f"""#!/usr/bin/env bash
set -e
cd {build_dir}
bash setup-build-env.sh
source env/bin/activate
if [ -f "environment.sh" ]; then
    source environment.sh
fi

mkdir -p {output_dir}
python extract_metadata_from_notebook.py --input "{self.filename}" --output "{metadata_path}"
jupyter nbconvert --debug --to html --execute "{self.filename}" --output "{html_path}" --ExecutePreprocessor.timeout=600
cd -
"""
        with open(build_script_filepath, 'w') as stream:
            stream.write(build_script)

class Category(typing.NamedTuple):
    name: str
    notebooks: typing.List[Notebook]
    source_dir: str
    build_dir: str
    artifact_dir: str

    def setup_build_env(self: PWN) -> None:
        env_setup_script: str = f"""#!/usr/bin/env bash
set -e
cd {self.build_dir}
virtualenv -p $(which python3) env
source env/bin/activate
pip install -U pip setuptools
if [ -f "pre-install.sh" ]; then
    bash pre-install.sh
fi
if [ -f "pre-requirements.txt" ]; then
    pip install -U -r pre-requirements.txt
fi
pip install -U -r requirements.txt
if [ -f "environment.sh" ]; then
    source environment.sh
fi
pip install jupyter
mkdir -p {self.artifact_dir}
cd -
"""
        os.makedirs(self.build_dir)
        os.makedirs(self.artifact_dir)
        build_script_path = os.path.join(self.build_dir, 'setup-build-env.sh')
        with open(build_script_path, 'w') as stream:
            stream.write(env_setup_script)

        for filename in ['environment.sh', 'pre-install.sh', 'pre-requirements.txt', 'requirements.txt']:
            build_filepath = os.path.join(self.build_dir, filename)
            source_filepath = os.path.join(self.source_dir, filename)
            if os.path.exists(source_filepath):
                shutil.copyfile(source_filepath, build_filepath)

        for filename in ['extract_metadata_from_notebook.py']:
            build_filepath = os.path.join(self.build_dir, filename)
            source_filepath = os.path.join(os.getcwd(), '.circleci', filename)
            if os.path.exists(source_filepath):
                shutil.copyfile(source_filepath, build_filepath)

class Collection(typing.NamedTuple):
    name: str
    categories: typing.List[Category]

class BuildJob(typing.NamedTuple):
    name: Category
    scripts: typing.List[str]

def build_categories(start_path: str) -> types.GeneratorType:
    for root, dirnames, filenames in os.walk(start_path):
        for dirname in dirnames:
            dirpath = os.path.join(root, dirname)
            notebooks = []
            for filepath in glob.glob(f'{dirpath}/*.ipynb'):
                name = os.path.basename(filepath).rsplit('.', 1)[0]
                rel_filepath = os.path.relpath(filepath)
                filename = os.path.basename(filepath)
                notebooks.append(Notebook(name, filename, rel_filepath))

            if notebooks:
                requirements_path = os.path.relpath(os.path.join(dirpath, 'requirements.txt'))
                if not os.path.exists(requirements_path):
                    raise NotImplementedError(f'Category missing Requirements File[{requirements_path}]')

                temp_name = os.path.basename(tempfile.NamedTemporaryFile().name)
                build_dir = os.path.join(BUILD_BASE_DIR, dirname)
                if os.path.exists(build_dir):
                    shutil.rmtree(build_dir)

                artifact_dir = os.path.join(ARTIFACT_DEST_DIR, dirname)
                if os.path.exists(artifact_dir):
                    shutil.rmtree(artifact_dir)

                yield Category(dirname, notebooks, dirpath, build_dir, artifact_dir)

            else:
                for category in build_categories(dirpath):
                    yield category

def build_collections() -> types.GeneratorType:
    for name in ['jdat_notebooks']:
        c_path = os.path.join(os.getcwd(), name)
        yield Collection(name, [cate for cate in build_categories(c_path)])

def find_build_jobs():
    for collection in build_collections():
        for category in collection.categories:
            category.setup_build_env()
            build_scripts = []
            for notebook in category.notebooks:
                notebook.create_build_script([collection.name, category.name], category.build_dir, category.artifact_dir)
                notebook.inject_notebook(category.build_dir)
                build_scripts.append(os.path.join(category.build_dir, f'{notebook.filename}-builder.sh'))

            yield BuildJob(category, build_scripts)


class BuildError(Exception):
    pass

def build_artifact(cmd: typing.Union[str, typing.List[str]]) -> None:
    if isinstance(cmd, str):
        cmd = [cmd]

    buffer_size = 1024
    proc = subprocess.Popen(cmd, shell=True)
    while proc.poll() is None:
        time.sleep(.1)

    if proc.poll() > 0:
        raise BuildError(f'Process Exit Code[{proc.poll()}]')

def main() -> None:
    pass_names = ['preimaging_01_mirage.ipynb-builder.sh']
    for build_job in find_build_jobs():
        for script in build_job.scripts:
            name = os.path.basename(script)
            if name in pass_names:
                continue

            command = f'bash {script}'
            try:
                build_artifact(command)
            except BuildError as err:
                logger.error(f'Unable to build {script}')
                import pdb; pdb.set_trace()
                pass


if __name__ in ['__main__']:
    main()


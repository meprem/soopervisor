"""
Export a Ploomber DAG to Airflow
"""
import shutil
from pathlib import Path
from itertools import chain

import click
from jinja2 import Environment, PackageLoader, StrictUndefined

from ploomber.spec import DAGSpec
from soopervisor.airflow.config import AirflowConfig
from ploomber.products import MetaProduct
from soopervisor import config
from soopervisor import abc
from soopervisor.script.script import generate_script


class AirflowExporter(abc.AbstractExporter):
    CONFIG_CLASS = AirflowConfig

    @staticmethod
    def _add(cfg, env_name):
        """Export Ploomber project to Airflow

        Generates a .py file that exposes a dag variable
        """
        click.echo('Exporting to Airflow...')
        project_root = Path('.').resolve()
        project_name = project_root.name

        Path(env_name).mkdir(exist_ok=True)

        env = Environment(loader=PackageLoader('soopervisor', 'assets'),
                          undefined=StrictUndefined)
        template = env.get_template('airflow.py')

        out = template.render(project_name=project_name, env_name=env_name)

        # maybe the rest should be the submit step?
        # add: creates airflow.py
        # submit: copies (latest) source code and renames env
        # maybe rename submit to export? some backends export and submit
        # but others just export
        # after adding airflow.py users may edit it (as long as they dont
        # change  the dag initialization that's fine)

        # generate script that declares the Airflow DAG
        path_out = Path(env_name, 'dags', project_name + '.py')
        path_out.parent.mkdir(exist_ok=True, parents=True)
        path_out.write_text(out)

        click.echo(
            f'Airflow DAG declaration saved to {str(path_out)!r}, you may '
            'edit the file to change the configuration if needed, '
            '(e.g., set the execution period)')

    @staticmethod
    def _validate(cfg, dag, env_name):
        """
        Validates a project before exporting as an Airflow DAG.
        This runs as a sanity check in the development machine
        """
        project_root = Path('.').resolve()

        env = (f'env.{env_name}.yaml'
               if Path(f'env.{env_name}.yaml').exists() else None)

        spec = DAGSpec.find(env=env, lazy_import=True)
        dag_airflow = spec.to_dag()
        dag_airflow.render()

        # if factory function, check it's decorated to load from env.yaml (?)

        # with the dag instance and using env.airflow.yaml, check that products
        # are not saved inside the projects root folder
        #  airflow continuously scans $AIRFLOW_HOME/dags/ for dag definitions
        # and
        # any extra files can break this process - maybe also show the products
        # to know where things will be saved when running using airflow

        # TODO: test when some products aren't files
        products = [dag_airflow[t].product for t in dag_airflow._iter()]
        products = chain(*([p] if not isinstance(p, MetaProduct) else list(p)
                           for p in products))

        # TODO: improve error message by showing task names for each invalid
        # product

        def resolve_product(product):
            """Converts a File product to str with absolute path
            """
            return str(Path(str(product)).resolve())

        products_invalid = [
            # products cannot be inside project root, convert to absolute
            # otherwise /../../ might cause false positives
            str(p) for p in products
            if resolve_product(p).startswith(str(project_root))
        ]

        # TODO: move this validation to the submit stage, since the
        # config may change from one deployment to another
        if products_invalid:
            products_invalid_ = '\n'.join(products_invalid)
            # TODO: replace {env} if None, must print the location of the
            # loaded env
            raise ValueError(
                f'The initialized DAG with "{env}" is '
                'invalid. Some products are located under '
                'the project\'s root folder, which is not allowed when '
                'deploying '
                'to Airflow. Modify your pipeline so all products are saved '
                f'outside the project\'s root folder "{project_root}". Fix '
                f'the following products:\n{products_invalid_}')

        # TODO: ignore non-files
        # TODO: also raise if relative paths - because we don't know where
        # the dag will be executed from

        # maybe instantiate with env.yaml and env.airflow.yaml to make sure
        # products don't clash?

        # check all products are prefixed with products root - this implies
        # that files should be absolute paths otherwise it's ambiguous -
        # should then we raise an arror if any product if defined with relative
        # paths?

    @staticmethod
    def _submit(cfg, env_name, until):
        """
        Copies the current source code to the target environment folder.
        The code along with the DAG declaration file can be copied to
        AIRFLOW_HOME for execution
        """
        project_root = Path('.').resolve()
        project_name = project_root.name

        project_root_airflow = Path(env_name, 'ploomber', project_name)

        # since we are copying the source code into a sub-directory
        # we must ignore the target directory to prevent an infinite recursion
        rel = project_root_airflow.resolve().relative_to(project_root)
        sub_dir = rel.parts[0]

        def ignore(src, names):
            dir_name = Path(src).resolve().relative_to(project_root)
            return names if str(dir_name).startswith(sub_dir) else []

        shutil.copytree(project_root, dst=project_root_airflow, ignore=ignore)

        # rename env.{env_name}.yaml if needed
        config.replace_env(env_name=env_name, target_dir=project_root_airflow)

        click.echo(
            f'Copied project source code to {str(project_root_airflow)!r}')


def spec_to_airflow(project_root, dag_name, env_name, airflow_default_args):
    """Initialize a Soopervisor project DAG and convert it to Airflow

    Notes
    -----
    This function is called by the DAG definition parsed by Airflow in
    {AIRFLOW_HOME}/dags
    """
    script_cfg = AirflowConfig.from_file_with_root_key(
        Path(project_root, 'soopervisor.yaml'),
        env_name=env_name,
    )

    # we use lazy_import=True here because this runs in the
    # airflow host and we should never expect that environment to have
    # the project environment configured, as its only purpose is to parse
    # the DAG
    dag = DAGSpec.find(lazy_import=True).to_dag()

    return _dag_to_airflow(dag, dag_name, script_cfg, airflow_default_args)


def _dag_to_airflow(dag, dag_name, script_cfg, airflow_default_args):
    """Convert a Ploomber DAG to an Airflow DAG

    Notes
    -----
    This function is called by the DAG definition parsed by Airflow in
    {AIRFLOW_HOME}/dags
    """
    # airflow *is not* a soopervisor dependency, moving the imports here to
    # prevent module not found errors for users who don't use airflow
    from airflow import DAG
    from airflow.operators.bash_operator import BashOperator

    project_root = Path('.').resolve()
    project_name = project_root.name

    dag_airflow = DAG(
        dag_name,
        default_args=airflow_default_args,
        description='Ploomber dag',
        schedule_interval=None,
    )

    for task_name in dag:
        # TODO: might be better to generate the script.sh script and embed
        # it in the airflow.py file directly, then users can edit it if
        # they want to
        cmd = generate_script(config=script_cfg,
                              project_name=project_name,
                              command=f'ploomber task {task_name}')

        task_airflow = BashOperator(task_id=task_name,
                                    bash_command=cmd,
                                    dag=dag_airflow)

    for task_name in dag:
        task_ploomber = dag[task_name]
        task_airflow = dag_airflow.get_task(task_name)

        for upstream in task_ploomber.upstream:
            task_airflow.set_upstream(dag_airflow.get_task(upstream))

    return dag_airflow

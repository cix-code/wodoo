import arrow
from pathlib import Path
import io
import traceback
import yaml
import json
import inquirer
import pipes
import tempfile
from datetime import datetime
from retrying import retry
from .wait import tcp as tcp_wait
import shutil
import click
import os
import subprocess
import time
import humanize
import sys
import docker
from threading import Thread
from queue import Queue
import inspect
from copy import deepcopy

class DBConnection(object):
    def __init__(self, dbname, host, port, user, pwd):
        assert dbname
        assert host
        assert port
        assert user
        assert pwd
        self.dbname = dbname
        self.host = host
        self.port = int(port)
        self.user = user
        self.pwd = pwd

    def shortstr(self):
        return "{}:{}/{}".format(self.host, self.port, self.dbname)

    def clone(self, dbname=None):
        result = deepcopy(self)
        if dbname:
            result.dbname = dbname
        return result

    def get_psyco_connection(self):
        import psycopg2
        conn = psycopg2.connect(
            dbname=self.dbname,
            user=self.user,
            password=self.pwd,
            host=self.host,
            port=self.port,
        )
        return conn

def __assert_file_exists(path, isdir=False):
    if not Path(path).exists():
        raise Exception("{} {} not found!".format(
            'Directory' if isdir else 'File',
            path
        ))


def __system(cmd, cwd=None, suppress_out=False, raise_exception=True,
             wait_finished=True, shell=False, logger=None, stdin=None,
             pipeout=None, redirect_std_out_to_file=None,
             progress=False, progress_every_seconds=1, env=None
             ):
    assert isinstance(cmd, list)
    cmd = list(map(str, cmd))

    STDPIPE, ERRPIPE, bufsize = subprocess.PIPE, subprocess.PIPE, 1
    if (not wait_finished and pipeout is None) or pipeout is False:
        STDPIPE, ERRPIPE, bufsize = None, None, -1
    if pipeout:
        bufsize = 4096
        suppress_out = True
    if env:
        for k, v in os.environ.items():
            if k not in env:
                env[k] = v
    proc = subprocess.Popen(cmd, shell=shell, stdout=STDPIPE, stderr=ERRPIPE, bufsize=bufsize, cwd=cwd, stdin=stdin, env=env or None)
    collected_errors = []

    def reader(pipe, q):
        try:
            with pipe:
                for line in io.TextIOWrapper(pipe, encoding='utf-8'):
                    q.put((pipe, line))
                q.put((pipe, ''))
        except Exception:
            msg = traceback.format_exc()
            click.echo(msg)
        finally:
            q.put(None)

    data = {
        'size_written': 0,
    }
    size_written = out_file = 0
    if redirect_std_out_to_file:
        out_file = open(redirect_std_out_to_file, 'w')
    out = []

    def heartbeat():
        while proc.returncode is None:
            time.sleep(progress_every_seconds)
            print("{} processed".format(humanize.naturalsize(data['size_written'])))

    def handle_output():
        try:
            err = proc.stderr
            q = Queue()
            Thread(target=reader, args=[proc.stdout, q]).start()
            Thread(target=reader, args=[proc.stderr, q]).start()
            for source, line in iter(q.get, None):
                if source != err:
                    out.append(line)
                else:
                    collected_errors.append(line.strip())
                if source == err or not suppress_out:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                if source != err:
                    data['size_written'] += len(line)
                if redirect_std_out_to_file:
                    if source != err:
                        out_file.write(line)

                if logger:
                    if source == err:
                        logger.error(line.strip())
                    else:
                        logger.info(line.strip())
        except Exception:
            msg = traceback.format_exc()
            click.echo(msg)
    if progress:
        t = Thread(target=heartbeat)
        t.daemonized = True
        t.start()

    if wait_finished or progress or redirect_std_out_to_file:
        t = Thread(target=handle_output)
        t.daemonized = True
        t.start()
        if wait_finished:
            t.join()

    if not wait_finished:
        return proc
    proc.wait()
    if out_file:
        out_file.close()
        print("{} Size: {}".format(redirect_std_out_to_file, humanize.naturalsize(size_written)))

    if proc.returncode and raise_exception:
        raise Exception("Error executing: {}\n{}".format(" ".join(cmd), collected_errors))
    return "".join(out)

def __safe_filename(name):
    name = name or ''
    for c in [':\\/+?*;\'" ']:
        name = name.replace(c, "_")
    return name

def __write_file(path, content):
    with open(path, 'w') as f:
        f.write(content)

def __append_line(path, line):
    if not Path(path).exists():
        content = ""
    else:
        with open(path, 'r') as f:
            content = f.read().strip()
    content += "\n" + line
    with open(path, 'w') as f:
        f.write(content)

def __read_file(path, error=True):
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        if not error:
            return""

def E2(name):
    if name.startswith("$"):
        name = name[1:]
    return os.getenv(name, "")

def __get_odoo_commit():
    from module_tools.odoo_config import MANIFEST
    commit = MANIFEST().get('odoo-commit', "")
    if not commit:
        raise Exception("No odoo commit defined.")
    return commit

def __execute_sql(connection, sql, fetchone=False, fetchall=False, notransaction=False, no_try=False):
    @retry(wait_random_min=500, wait_random_max=800, stop_max_delay=30000)
    def try_connect():
        __execute_sql(connection, "SELECT * FROM pg_catalog.pg_tables;", 'template1', no_try=True)
    if not no_try:
        try_connect()

    conn = connection.get_psyco_connection()
    conn.autocommit = notransaction
    result = None
    cr = conn.cursor()
    try:
        cr.execute(sql)
        if fetchone:
            result = cr.fetchone()
        elif fetchall:
            result = cr.fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cr.close()
        conn.close()
    return result

def __exists_db(conn):
    sql = "select count(*) from pg_database where datname='{}'".format(conn.dbname)
    conn = conn.clone()
    conn.dbname = 'template1'
    record = __execute_sql(conn, sql, fetchone=True)
    if not record or not record[0]:
        return False
    return True


def __exists_table(conn, table_name):
    record = __execute_sql(conn, """
    select exists(
        select 1
        from information_schema.tables
        where table_name = '{}'
    )
    """.format(table_name), fetchone=True)
    return record[0]

def __start_postgres_and_wait(config):
    if config.run_postgres:
        if config.run_postgres_in_ram and __is_container_running('postgres'):
            # avoid recreate
            pass
        else:
            __dc(["up", "-d", "postgres"])
        conn = config.get_odoo_conn().clone(dbname='template1')
        __wait_for_port(conn.host, conn.port, timeout=30)
        __execute_sql(conn, sql="""
        SELECT table_schema,table_name
        FROM information_schema.tables
        ORDER BY table_schema,table_name;
        """)

def __is_container_running(machine_name):
    container_id = __dc(['ps', '-q', machine_name], raise_exception=False, suppress_out=True).strip()
    if container_id:
        container = list(filter(lambda container: container.id == container_id, docker.from_env().containers.list()))
        if container:
            container = container[0]
            return container.status == 'running'
    return False

def is_up(*machine_name):
    assert len(machine_name) == 1
    click.echo('Running' if __is_container_running(machine_name[0]) else 'Not Running', machine_name[0])

def __isfloat(x):
    try:
        float(x)
    except Exception:
        return False
    else:
        return True

def __makedirs(path):
    path.mkdir(exist_ok=True, parents=True)

def __remove_postgres_connections(connection, sql_afterwards=""):
    click.echo("Removing all current connections from {}".format(connection.dbname))
    if __exists_db(connection):
        SQL = """
            SELECT pg_terminate_backend(pg_stat_activity.pid)
            FROM pg_stat_activity
            WHERE pg_stat_activity.datname = '{}'
            AND pid <> pg_backend_pid();
        """.format(connection.dbname, sql_afterwards)
        __execute_sql(connection.clone(dbname='template1'), SQL, notransaction=True)
        if sql_afterwards:
            __execute_sql(connection.clone(dbname='template1'), sql_afterwards, notransaction=True)

def __rename_db_drop_target(conn, from_db, to_db):
    if 'to_db' == 'template1':
        raise Exception("Invalid: {}".format(to_db))
    __remove_postgres_connections(conn.clone(dbname=from_db))
    __remove_postgres_connections(conn.clone(dbname=to_db))
    __execute_sql(conn.clone(dbname='template1'), "drop database if exists {to_db}".format(**locals()), notransaction=True)
    __execute_sql(conn.clone(dbname='template1'), "alter database {from_db} rename to {to_db};".format(**locals()), notransaction=True)
    __remove_postgres_connections(conn.clone(dbname=to_db))

def __dc(cmd, suppress_out=False, raise_exception=True, wait_finished=True, logger=None, env={}):
    from . import dirs
    c = __get_cmd() + cmd
    out = __system(
        c,
        cwd=dirs['odoo_home'],
        suppress_out=suppress_out,
        raise_exception=raise_exception,
        logger=logger,
        env=env,
    )
    return out

def __dcexec(cmd, suppress_out=False):
    from . import dirs
    c = __get_cmd()
    c = c + ['exec', '-T'] + cmd
    out = __system(c, cwd=dirs['odoo_home'], suppress_out=suppress_out)
    return out

def __dcrun(cmd, interactive=False, raise_exception=True, logger=None, env={}):
    cmd2 = [os.path.expandvars(x) for x in cmd]
    cmd = ['run']
    if not interactive:
        cmd += ['-T']
    cmd += ['--rm', '-eODOO_HOME=/opt/odoo'] + cmd2
    return __dc(cmd, raise_exception=True, logger=logger, env=env)

def _askcontinue(config, msg=None):
    if msg:
        click.echo(msg)
    if config and config.force:
        return
    input("Continue? (Ctrl+C to break)")

def __set_db_ownership(config):
    # in development environments it is safe to set ownership, so
    # that accidently accessing the db fails
    if config.devmode:
        if config.run_postgres:
            __start_postgres_and_wait(config)
        from module_tools.module_tools import set_ownership_exclusive
        set_ownership_exclusive()

def __wait_for_port(host, port, timeout=None):
    res = tcp_wait.open(port, host=host, timeout=timeout)
    if not res and timeout:
        raise Exception("Timeout elapsed waiting for {}:{}".format(host, port))

def __replace_in_file(filepath, text, replacewith):
    with open(filepath, 'r') as f:
        content = f.read()
    content = content.replace(text, replacewith)
    with open(filepath, 'w') as f:
        f.write(content)

def __rm_file_if_exists(path):
    if path.exists():
        path.unlink()

def __rmtree(path):
    from . import dirs
    if not path or path == '/':
        raise Exception("Not allowed: {}".format(path))
    if not path.startswith("/"):
        raise Exception("Not allowed: {}".format(path))
    if not any(path.startswith(dirs['odoo_home'] + x) for x in ['/tmp', '/run/']):
        if "/tmp" in path:
            pass
        else:
            raise Exception('not allowed')
    shutil.rmtree(path)

def __safeget(array, index, exception_on_missing, file_options=None):
    if file_options:
        if file_options.exists():
            file_options = '\n' + '\n'.join(file_options.glob("*"))
    file_options = file_options or ''
    if len(array) < index + 1:
        raise Exception(exception_on_missing + file_options)
    return array[index]

def __get_cmd():
    from . import commands
    cmd = commands['dc']
    cmd = [os.path.expandvars(x) for x in cmd]
    return cmd

def __cmd_interactive(*params):
    cmd = __get_cmd() + list(params)
    proc = subprocess.Popen(cmd)
    proc.wait()
    # ctrl+c leads always to error otherwise
    # if proc.returncode:
    # raise Exception("command failed: {}".format(" ".join(params)))

def __empty_dir(dir):
    dir = Path(dir)
    if dir.is_dir():
        shutil.rmtree(dir)

def __file_default_content(path, default_content):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_content)

def __file_get_lines(path):
    return path.read_text().strip().split("\n")

def _get_machines():
    from . import commands, dirs
    cmd = commands['dc'] + ['ps', '--services']
    out = __system(cmd, cwd=dirs['odoo_home'], suppress_out=True)
    out = set(filter(lambda x: x, out.split("\n")))
    return list(sorted(out))

def _get_platform():
    from . import PLATFORM_OSX, PLATFORM_LINUX
    if os.getenv("PLATFORM", "") in ['Darwin', 'OSX', 'macos']:
        return PLATFORM_OSX
    else:
        return PLATFORM_LINUX

@retry(wait_random_min=500, wait_random_max=800, stop_max_delay=30000)
def __get_docker_image():
    """
    Sometimes this command fails; checked with pudb behind call, hostname matches
    container id; seems to be race condition or so
    """
    hostname = os.environ['HOSTNAME']
    result = [x for x in __system(["/opt/docker/docker", "inspect", hostname], suppress_out=True).split("\n") if "\"Image\"" in x]
    if result:
        result = result[0].split("sha256:")[-1].split('"')[0]
        return result[:12]
    return None

def _file2env(filepath, out_dict=None):
    from . import MyConfigParser
    if not filepath.exists():
        return
    config = MyConfigParser(filepath)
    for k in config.keys():
        if out_dict:
            out_dict[k] = config[k]
        else:
            os.environ[k] = config[k]

def _get_bash_for_machine(machine):
    if machine == 'postgres':
        return 'bash'
    else:
        return 'bash'


def _remember_customs_and_cry_if_changed(config):
    # if customs changed, then restart is required

    if __is_container_running('odoo'):
        out = __dcexec(['odoo', 'env'], suppress_out=True)
        out = [x for x in out.split('\n') if x.startswith("CUSTOMS=")]
        if out:
            current_customs = out[0].split("=")[-1]
            if config.customs:
                if current_customs != config.customs:
                    click.echo("Customs changed - you need to restart and/or rebuild!")
                    __dc(['stop', '-t 2'])

def _remove_temp_directories():
    from . import dirs
    for dir in os.listdir(dirs['odoo_home']):
        if dir.startswith("tmp") and len(dir) == len('tmp......'):
            __rmtree(dirs['odoo_home'] / dir)

def _prepare_filesystem():
    from . import dirs, files
    __makedirs(dirs['settings.d'])
    for subdir in ['config', 'sqlscripts', 'debug', 'proxy']:
        __makedirs(dirs['odoo_home'] / 'run' / 'subdir')
    __system(['sudo', '-E', 'chown', "{uid}:{uid}".format(uid=os.getenv("UID")), "-R", dirs['run']])

    __file_default_content(files['odoo_instances'], "default default\n")

def _sanity_check(config):
    from . import files
    if config.run_postgres is None:
        raise Exception("Please define RUN_POSTGRES")

    if config.run_postgres and config.db_host != 'postgres':
        click.echo("You are using the docker postgres container, but you do not have the DB_HOST set to use it.")
        click.echo("Either configure DB_HOST to point to the docker container or turn it off by: ")
        click.echo("RUN_POSTGRES=0")
        sys.exit(1)

    if not config.owner_uid:
        click.echo("Advise: you should set OWNER_UID so that dump files are marked as the correct owner")
        time.sleep(3)

    if config.odoo_files and Path(config.odoo_files).is_dir():
        if config.owner_uid and Path(config.odoo_files).stat().st_uid != config.owner_uid_as_int:
            _fix_permissions()

    # make sure the odoo_debug.txt exists; otherwise directory is created
    __file_default_content(files['run/odoo_debug.txt'], "")

def __get_installed_modules(config):
    conn = config.get_odoo_conn()
    rows = __execute_sql(
        conn,
        sql="SELECT name, state from ir_module_module where state in ('installed', 'to upgrade');",
        fetchall=True
    )
    return [x[0] for x in rows]

def __splitcomma(param):
    if isinstance(param, str):
        if not param:
            return []
        return [x.strip() for x in param.split(',') if x.strip()]
    elif isinstance(param, (tuple, list)):
        return list(param)
    raise Exception("not impl")

def __try_to_set_owner(UID, path, recursive=False):
    if path.is_dir():
        uid = os.stat(path).st_uid
        if str(uid) != str(UID) or recursive:
            click.echo("Trying to set correct permissions on {}".format(path))
            options = ""
            if recursive:
                options += "-R"

            __system([
                'sudo',
                'find',
                '-not',
                '-type',
                'l',
                '-not',
                '-user',
                UID,
                '-exec',
                'chown',
                UID,
                '{}',
                '+'
            ], cwd=path)

def _check_working_dir_customs_mismatch(config):
    # Checks wether the current working is in a customs directory, but
    # is not matching the correct customs. Avoid creating wrong tickets
    # in the wrong customizations.

    from . import dirs
    for working_dir in dirs['host_working_dir'].parents:
        if (working_dir / 'MANIFEST').is_file():
            break
    else:
        return # no customs

    current_customs = working_dir.name
    if current_customs != config.customs:
        _askcontinue(None, """Caution: current customs is {} but you are in another customs directory: {}
Continue at your own risk!""".format("$CUSTOMS", "$LOCAL_WORKING_DIR")
                     )

def _display_machine_tips(machine_name):
    from . import dirs
    dir = dirs['machines'] / machine_name
    if not dir.is_dir():
        return

    for filename in dirs['machines'].glob("**/tips.txt"):
        filepath = dirs['machines'] / filename
        if filepath.parent.name == machine_name:
            content = (dirs['machines'] / filename).read_text()
            click.echo("")
            click.echo("Please note:")
            click.echo("---------------")
            click.echo("")
            click.echo(content)
            click.echo("")
            click.echo("")

def __do_command(cmd, *params, **kwparams):
    cmd = cmd.replace('-', '_')
    return globals()[cmd](*params, **kwparams)

def _fix_permissions(config):
    from . import odoo_config
    if config.odoo_files and Path(config.odoo_files).is_dir() and \
            config.owner_uid and \
            config.owner_uid_as_int != 0:
        __try_to_set_owner(config.owner_uid, Path(config.odoo_files), recursive=True)
    customs_dir = odoo_config.customs_dir()
    __try_to_set_owner("1000", customs_dir, recursive=True) # so odoo user has access

def _get_dump_files(backupdir, fnfilter=None):
    _files = list(backupdir.glob("*"))

    def _get_ctime(filepath):
        if fnfilter and fnfilter not in filepath.name:
            return False
        try:
            return (backupdir / filepath).stat().st_ctime
        except Exception:
            return 0
    rows = []
    for i, file in enumerate(sorted(filter(lambda x: _get_ctime(x), _files), reverse=True, key=_get_ctime)):
        filepath = backupdir / file
        delta = arrow.get() - arrow.get(filepath.stat().st_mtime)
        rows.append((
            i + 1,
            file.name,
            humanize.naturaltime(delta),
            humanize.naturalsize(filepath.stat().st_size),
        ))

    return rows

def __postgres_restore(conn, filepath):
    __assert_file_exists(filepath)

    click.echo("Restoring dump on {}:{} as {}".format(conn.host, conn.port, conn.user))
    os.environ['PGPASSWORD'] = conn.pwd
    args = ["-h", conn.host, "-p", str(conn.port), "-U", conn.user]
    PGRESTORE = [
        "pg_restore",
        "--no-owner",
        "--no-privileges",
        "--no-acl",
    ] + args
    PSQL = ["psql"] + args

    dbname = conn.dbname
    conn = conn.clone(dbname='template1')
    __execute_sql(conn, "drop database if exists {}".format(dbname), notransaction=True)
    __execute_sql(conn, "create database {}".format(dbname), notransaction=True)

    method = PGRESTORE
    needs_unzip = True

    dump_type = __get_dump_type(filepath)
    if dump_type == 'plain_text':
        needs_unzip = False
        method = PSQL
    elif dump_type == 'zipped_sql':
        method = PSQL
        needs_unzip = True
    elif dump_type == "zipped_pgdump":
        pass
    elif dump_type == "pgdump":
        needs_unzip = False
    else:
        raise Exception("not impl: {}".format(dump_type))

    PREFIX = []
    if needs_unzip:
        PREFIX = ["/bin/gunzip"]
    else:
        PREFIX = []
    started = datetime.now()
    click.echo("Restoring DB...")
    CMD = " " .join(pipes.quote(s) for s in ['pv', str(filepath)])
    CMD += " | "
    if PREFIX:
        CMD += " ".join(pipes.quote(s) for s in PREFIX)
        CMD += " | "
    CMD += " ".join(pipes.quote(s) for s in method)
    CMD += " "
    CMD += " ".join(pipes.quote(s) for s in [
        '-d',
        dbname,
    ])
    os.system(CMD)
    click.echo("Restore took {} seconds".format((datetime.now() - started).seconds))

def __get_dump_type(filepath):
    temp = Path(tempfile.mktemp(suffix='.check'))
    MARKER = "PostgreSQL database dump"
    FNULL = open(os.devnull, 'w')
    proc = subprocess.Popen(['gunzip', '-c', filepath], stdout=subprocess.PIPE, stderr=FNULL, bufsize=1)

    def reader(proc, pipe):
        try:
            lines = 0
            with pipe:
                for line in iter(pipe.readline, ''):
                    with temp.open('a') as f:
                        f.write(line.decode("utf-8", errors='ignore'))
                        lines += 1
                        if lines > 20:
                            break
        finally:
            if not proc.returncode:
                proc.kill()

    Thread(target=reader, args=[proc, proc.stdout]).start()
    proc.wait()

    if temp.exists():
        content = temp.read_text()
        if MARKER in content:
            return 'zipped_sql'
        if content.startswith("PGDMP"):
            return "zipped_pgdump"
    with open(filepath, 'r') as f:
        for i, line in enumerate(f):
            if i == 0 and line.startswith("PGDMP"):
                return 'pgdump'
            if i > 50:
                break
            if MARKER in line:
                return "plain_text"
    return 'unzipped_pgdump'

def _dropdb(config, conn):
    if __exists_db(conn):
        # TODO ask for name
        if not config.force:
            questions = [
                inquirer.Text('name',
                              message="Database {} will be dropped. Please enter the name to delete it ({})".format(conn.dbname, conn.shortstr()))
            ]
            answer = inquirer.prompt(questions)
            if answer['name'] != conn.dbname:
                raise Exception("Dropping aborted")
    else:
        click.echo("Database does not exist yet: {}".format(conn.dbname))
    click.echo("Stopping all services and creating new database")
    __remove_postgres_connections(conn, 'drop database {};'.format(conn.dbname))

    click.echo("Database dropped {}".format(conn.dbname))

def __backup_postgres(conn, filepath):
    filepath = Path(filepath)
    os.environ['PGPASSWORD'] = conn.pwd
    psyconn = conn.get_psyco_connection()
    cr = psyconn.cursor()
    cr.execute("SELECT (pg_database_size(current_database())) FROM pg_database")
    size = cr.fetchone()[0] * 0.7 # ct
    bytes = str(float(size)).split(".")[0]
    psyconn.close()
    temp_filepath = filepath.with_name('.' + filepath.name)
    os.system('pg_dump -h "{host}" -p {port} -U "{user}" -Z0 -Fc {dbname} | pv -s {bytes} | pigz --rsyncable > {filepath}'.format(
        host=conn.host,
        port=conn.port,
        user=conn.user,
        dbname=conn.dbname,
        bytes=bytes,
        filepath=temp_filepath,
    ))
    temp_filepath.replace(filepath)

def remove_webassets(conn):
    click.echo("Removing web assets")
    conn = conn.get_psyco_connection()
    cr = conn.cursor()
    try:
        cr.execute("delete from ir_attachment where name ilike '/web/%web%asset%'")
        cr.execute("delete from ir_attachment where name ilike 'import_bootstrap.less'")
        cr.execute("delete from ir_attachment where name ilike '%.less'")
        cr.execute("delete from ir_attachment where name ilike 'web_icon_data'")
        conn.commit()
    finally:
        cr.close()
        conn.close()

def get_dockercompose():
    from . import files
    content = __read_file(files['docker_compose'])
    compose = yaml.safe_load(content)
    return compose

def get_volume_names():
    vols = get_dockercompose()['volumes'].keys()
    project_name = os.environ['PROJECT_NAME']
    return ["{}_{}".format(project_name, x) for x in vols]

def __running_as_root_or_sudo():
    output = subprocess.check_output(["/usr/bin/id", '-u']).strip().decode('utf-8')
    return output == "0"

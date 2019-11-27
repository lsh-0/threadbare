import subprocess
import getpass
from pssh.clients.native import SSHClient as PSSHClient
from pssh import exceptions as pssh_exceptions
import os, sys
from threadbare import state
from threadbare.common import merge, subdict, rename

class SSHClient(PSSHClient):
    # do not copy.deepcopy the pssh SSHClient object, just
    # return a reference to the object (self)
    # - https://docs.python.org/3/library/copy.html
    def __deepcopy__(self, memo):
        return self

class NetworkError(BaseException):
    """generic 'died while doing something ssh-related' catch-all exception class.
    calling str() on this exception will return the results on calling str() on the 
    wrapped exception."""
    def __init__(self, wrapped_exception_inst):
        self.wrapped = wrapped_exception_inst

    def __str__(self):
        # we have the opportunity here to tweak the error messages to make them
        # similar with their equivalents in Fabric.
        # original error messages are still available via `str(exinst.wrapped)`
        space = " "
        custom_error_prefixes = {
            # builder: https://github.com/elifesciences/builder/blob/master/src/buildercore/core.py#L345-L347
            # pssh: https://github.com/ParallelSSH/parallel-ssh/blob/8b7bb4bcb94d913c3b7da77db592f84486c53b90/pssh/clients/native/parallel.py#L272-L274
            pssh_exceptions.Timeout: "Timed out trying to connect." + space,

            # builder: https://github.com/elifesciences/builder/blob/master/src/buildercore/core.py#L348-L350
            # fabric: https://github.com/mathiasertl/fabric/blob/master/fabric/network.py#L601-L605
            # pssh: https://github.com/ParallelSSH/parallel-ssh/blob/2e9668cf4b58b38316b1d515810d7e6c595c76f3/pssh/exceptions.py
            pssh_exceptions.SSHException: "Low level socket error connecting to host." + space,
            pssh_exceptions.SessionError: "Low level socket error connecting to host." + space,
            pssh_exceptions.ConnectionErrorException: "Low level socket error connecting to host." + space,
        }
        new_error = custom_error_prefixes.get(type(self.wrapped)) or ""
        original_error = str(self.wrapped)
        return new_error + original_error

# utils

# direct copy from Fabric:
# https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L33-L46
# TODO: adjust licence accordingly
def _shell_escape(string):
    """
    Escape double quotes, backticks and dollar signs in given ``string``.
    For example::
        >>> _shell_escape('abc$')
        'abc\\\\$'
        >>> _shell_escape('"')
        '\\\\"'
    """
    for char in ('"', '$', '`'):
        string = string.replace(char, r'\%s' % char)
    return string

# https://github.com/mathiasertl/fabric/blob/master/fabric/state.py#L253-L256
def shell_wrap_command(command):
    """wraps the given command in a shell invocation.
    default shell is /bin/bash (like Fabric)
    no support for configurable shell at present"""

    # '-l' is 'login' shell
    # '-c' is 'run command'
    shell_prefix = "/bin/bash -l -c"

    escaped_command = _shell_escape(command)
    escaped_wrapped_command = '"%s"' % escaped_command

    space = " "
    final_command = shell_prefix + space + escaped_wrapped_command

    return final_command

def sudo_wrap_command(command):
    """adds a 'sudo' prefix to command to run as root. 
    no support for sudo'ing to configurable users/groups"""
    # https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L605-L623
    # https://github.com/mathiasertl/fabric/blob/master/fabric/state.py#L374-L376
    # note: differs from Fabric. they support interactive input of password, users and groups
    # we use it exclusively to run commands as root
    sudo_prefix = "sudo --non-interactive"
    space = " "
    return sudo_prefix + space + command

def handle(base_kwargs, kwargs):
    key_list = base_kwargs.keys()
    global_kwargs = subdict(state.ENV, key_list)
    user_kwargs = subdict(kwargs, key_list)
    final_kwargs = merge(base_kwargs, global_kwargs, user_kwargs)
    return global_kwargs, user_kwargs, final_kwargs

# api

def _ssh_client(**kwargs):
    """returns an instance of pssh.clients.native.SSHClient
    if within a state context, looks for a client already in use and returns that if found.
    if not found, creates a new one and stores it for later use."""
    
    # parameters we're interested in and their default values
    base_kwargs = {
        # current user. sensible default but probably not what you want
        'user': getpass.getuser(),
        'host_string': None,
        'key_filename': os.path.expanduser("~/.ssh/id_rsa"),
        'port': 22,
    }
    global_kwargs, user_kwargs, final_kwargs = handle(base_kwargs, kwargs)
    final_kwargs['password'] = None # always private keys
    rename(final_kwargs, [('key_filename', 'pkey'), ('host_string', 'host')])

    # if we're not using global state, return the new client as-is
    env = state.ENV
    if env.read_only:
        return SSHClient(**final_kwargs)

    client_map_key = "ssh_client"
    client_key = tuple(sorted(final_kwargs.items()))
    
    # otherwise, check to see if a previous client is available
    client_map = env.get(client_map_key, {})
    if client_key in client_map:
        return client_map[client_key]

    # if not, create a new one and store it in the state

    # https://parallel-ssh.readthedocs.io/en/latest/native_single.html#pssh.clients.native.single.SSHClient
    client = SSHClient(**final_kwargs)

    # disconnect session when leaving context manager
    state.add_cleanup(lambda: client.disconnect())

    client_map[client_key] = client
    env[client_map_key] = client_map

    return client

# todo: 'api.py' and '__init__.py' are poorly named and this function + a `local` function
# should probably be wrapped `__init__/execute`
def _execute(command, user, key_filename, host_string, port, use_pty):
    """creates an SSHClient object and executes given `command` with the given parameters.
    it does not consult global state and all parameters must be explicitly passed in.
    keep this function as simple as possible."""

    client = _ssh_client(user=user, host_string=host_string, key_filename=key_filename, port=port)
    
    # https://github.com/ParallelSSH/parallel-ssh/blob/1.9.1/pssh/clients/native/single.py#L408
    sudo = False # handled ourselves
    shell = False # handled ourselves
    timeout = None # todo
    encoding = 'utf-8' # default everywhere

    try:
        channel, host_string, stdout, stderr, stdin = client.run_command(command, sudo, user, use_pty, shell, encoding, timeout)

        def get_exitcode():
            """we can't know the exit code until command has finished running but we *can* access
            the output streams. attempting to realise the exitcode will cause the thread of execution 
            to block until the channel is finished"""
            client.wait_finished(channel) # `timeout` here
            return channel.get_exit_status()

        return {
            'return_code': get_exitcode,
            'command': command,
            'stdout': stdout,
            'stderr': stderr,
        }
    except BaseException as ex:
        # *most likely* a network error:
        # https://github.com/ParallelSSH/parallel-ssh/blob/master/pssh/exceptions.py
        raise NetworkError(ex)

def _print_line(output_pipe, quiet, discard_output, line):
    """writes the given `line` (string) to the given `output_pipe` (file-like object)
    if `quiet` is False, `line` is not written.
    if `discard_output` is False, `line` is not returned.
    `discard_output` should be set to `True` when you're expecting very large responses."""
    if not quiet:
        output_pipe.write(line + "\n")
    if not discard_output:
        return line

def _process_output(output_pipe, result_list, quiet, discard_output):
    "calls `_print_line` on each result in `result_list`."
    kwargs = subdict(locals(), ['quiet', 'discard_output'])

    # always process the results as soon as we have them
    # use `quiet` to hide the printing of output to stdout/stderr
    # use `discard_output` to discard the results as soon as they are read
    # stderr may be empty if `combine_stderr` in `remote` was `True`
    new_results = [_print_line(output_pipe, line=line, **kwargs) for line in result_list]
    output_pipe.flush()
    if not kwargs['discard_output']:
        return new_results

# https://github.com/mathiasertl/fabric/blob/master/fabric/state.py#L338
# https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L898-L901
# https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L975
def remote(command, **kwargs):
    "preprocesses given `command` and options before sending it to `_execute` to be executed on remote host"

    # Fabric function signature for `run`
    #shell=True # done
    #pty=True   # mutually exclusive with combine_stderr. not sure what Fabric/Paramiko is doing here
    #combine_stderr=None # mutually exclusive with use_pty. 'True' in global env.
    #quiet=False, # done
    #warn_only=False # ignore
    #stdout=None # done, stdout/stderr always available unless explicitly discarded. 'see discard_output'
    #stderr=None # done, stderr not available when combine_stderr is `True`
    #timeout=None # todo
    #shell_escape=None # ignored. shell commands are always escaped
    #capture_buffer_size=None # correlates to `ssh2.channel.read` and the `size` parameter. Ignored.

    # parameters we're interested in and their default values
    base_kwargs = {
        # current user. sensible default but probably not what you want
        'user': getpass.getuser(),
        'host_string': None,
        'key_filename': os.path.expanduser("~/.ssh/id_rsa"),
        'port': 22,
        'use_shell': True,
        'use_sudo': False,
        'combine_stderr': True,
        'quiet': False,
        'discard_output': False,
    }
    global_kwargs, user_kwargs, final_kwargs = handle(base_kwargs, kwargs)
    
    # wrap the command up
    # https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L920-L925
    if final_kwargs['use_shell']:
        command = shell_wrap_command(command)
    if final_kwargs['use_sudo']:
        command = sudo_wrap_command(command)
        
    # if use_pty is True, stdout and stderr are combined and stderr will yield nothing.
    # - https://parallel-ssh.readthedocs.io/en/latest/advanced.html#combined-stdout-stderr
    use_pty = final_kwargs['combine_stderr']
    
    # values `remote` specifically passes to `_execute`
    execute_kwargs = {
        'command': command,
        'use_pty': use_pty
    }
    execute_kwargs = merge(final_kwargs, execute_kwargs)
    execute_kwargs = subdict(execute_kwargs, ['command', 'user', 'key_filename', 'host_string', 'port', 'use_pty'])
    # TODO: validate `_execute`s args. `host_string` can't be None for example

    #print('final kwargs',execute_kwargs)
    
    # run command
    result = _execute(**execute_kwargs)

    # handle stdout/stderr streams
    output_kwargs = subdict(final_kwargs, ['quiet', 'discard_output'])
    result.update({
        'stdout': _process_output(sys.stdout, result['stdout'], **output_kwargs),
        'stderr': _process_output(sys.stderr, result['stderr'], **output_kwargs),

        # command must have finished before we have access to return code
        'return_code': result['return_code'](), 
    })

    return result

# https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L1100
def remote_sudo(command, **kwargs):
    "exactly the same as `remote`, but the given command is run as the root user"
    # user=None  # ignore
    # group=None # ignore
    kwargs['use_sudo'] = True
    return remote(command, **kwargs)

# https://github.com/mathiasertl/fabric/blob/master/fabric/contrib/files.py#L15
def remote_file_exists(path, **kwargs):
    "returns True if given path exists on remote system"
    # note: Fabric is doing something weird and clever here:
    # - https://github.com/mathiasertl/fabric/blob/master/fabric/contrib/files.py#L474-L485
    # but their examples don't work:

    # $ /bin/sh
    # sh-5.0$ foo="$(echo /usr/\*/share)"
    # sh-5.0$ echo $foo
    # /usr/*/share
    # sh-5.0$ exit
    # $ echo $SHELL
    # $ /bin/bash
    # $ foo="$(echo /usr/\*/share)"
    # $ echo $foo
    # /usr/*/share

    base_kwargs = {
        'use_sudo': False,
    }
    global_kwargs, user_kwargs, final_kwargs = handle(base_kwargs, kwargs)
    remote_fn = remote_sudo if final_kwargs['use_sudo'] else remote
    command = "test -e %s" % path
    return remote_fn(command, **kwargs)['return_code'] == 0

# https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L1157
def local(command, **kwargs):
    base_kwargs = {
        'use_shell': True,
        'combine_stderr': True,
        'capture': False,
    }
    global_kwargs, user_kwargs, final_kwargs = handle(base_kwargs, kwargs)

    if final_kwargs['capture']:
        if final_kwargs['combine_stderr']:
            out_stream = subprocess.PIPE
            err_stream = subprocess.STDOUT
        else:
            out_stream = subprocess.PIPE
            err_stream = subprocess.PIPE
    else:
        out_stream = None
        err_stream = None

    if not final_kwargs['use_shell'] and not isinstance(command, list):
        raise ValueError("when shell=False, given command *must* be a list")
        
    if final_kwargs['use_shell']:
        command = shell_wrap_command(command)

    p = subprocess.Popen(command, shell=final_kwargs['use_shell'], stdout=out_stream, stderr=err_stream)
    stdout, stderr = p.communicate()

    # https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L1240-L1244    
    return {
        'return_code': p.returncode,
        'failed': p.returncode > 0,
        'succeeded': p.returncode == 0,
        'command': command,
        'stdout': (stdout or b'').decode('utf-8').splitlines(),
        'stderr': (stderr or b'').decode('utf-8').splitlines(),
    }

def single_command(cmd_list):
    """given a list of commands to run, returns a single command
    `remote` and `local` are expected to do any escaping as necessary"""
    if cmd_list in [None, []]:
        return None
    return ' && '.join(map(str, cmd_list))

# https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L419
# use_sudo hack: https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L453-L458
def _download_as_root_hack(remote_path, local_path, **kwargs):
    """as root, creates a temporary copy of the file that can be downloaded by a 
    regular user and then removes the temporary file.
    warning: don't try to download anything huge `with_sudo` as the file is duplicated.
    warning: the privileged file will be available in /tmp until the download is complete"""
    
    if not remote_file_exists(remote_path, use_sudo=True, **kwargs):
        raise EnvironmentError("remote file does not exist: %s" % (remote_path,))
    client = _ssh_client(**kwargs)

    cmd = single_command([
        # create a temporary file with the suffix '-threadbare'
        'tempfile=$(mktemp --suffix "-threadbare")',
        # copy the target file to this temporary file
        'cp "%s" "$tempfile"' % remote_path,
        # ensure it's readable by the user doing the downloading
        'chmod +r "$tempfile"',
        # emit the name of the temporary file so we can find it to download it
        'echo "$tempfile"'
    ])
    result = remote_sudo(cmd)
    remote_tempfile=result['stdout'][-1]
    #assert remote_file_exists(remote_tempfile, use_sudo=True, **kwargs) # sanity check
    remote_path = remote_tempfile
    client.copy_remote_file(remote_tempfile, local_path)
    remote_sudo('rm "%s"' % remote_tempfile)
    return local_path

# https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L419
# use_sudo hack: https://github.com/mathiasertl/fabric/blob/master/fabric/operations.py#L453-L458
def download(remote_path, local_path, use_sudo=False, **kwargs):
    """downloads file at `remote_path` to `local_path`, overwriting the local path if it exists.
    avoid `use_sudo` if at all possible"""

    # ensure the output of any remote commands gets hidden
    with state.settings(quiet=True):
        if use_sudo:
            return _download_as_root_hack(remote_path, local_path, **kwargs)
    
        if not remote_file_exists(remote_path, **kwargs):
            raise EnvironmentError("remote file does not exist: %s" % (remote_path,))

        client = _ssh_client(**kwargs)
        client.copy_remote_file(remote_path, local_path)
        return local_path

def _upload_as_root_hack(local_path, remote_path, **kwargs):
    """uploads file at `local_path` to a remote temporary file then moves the file to `remote_path` as root.
    does not alter any permissions or attributes on the file"""

    client = _ssh_client(**kwargs)

    cmd = single_command([
        # create a temporary file with the suffix '-threadbare'
        'tempfile=$(mktemp --suffix "-threadbare")',
        'echo "$tempfile"'
    ])
    result = remote(cmd)
    remote_temp_path = result['stdout'][-1]
    assert remote_file_exists(remote_temp_path, **kwargs) # sanity check

    client.copy_file(local_path, remote_temp_path)
    remote_sudo('mv "%s" "%s"' % (remote_temp_path, remote_path))
    assert remote_file_exists(remote_path, use_sudo=True)

def upload(local_path, remote_path, use_sudo=False, **kwargs):
    "uploads file at `local_path` to the given `remote_path`, overwriting anything that may be at that path"
    with state.settings(quiet=True):
        if use_sudo:
            return _upload_as_root_hack(local_path, remote_path, **kwargs)

        if not os.path.exists(local_path):
            raise EnvironmentError("local file does not exist: %s" % (local_path,))

        # you're not crazy, sftp is *exceptionally* slow:
        # - https://github.com/ParallelSSH/parallel-ssh/issues/177
        #local('du -sh %s' % local_path)
        #client = _ssh_client(timeout=5, keepalive_seconds=1, num_retries=1, **kwargs)
        client = _ssh_client(**kwargs)
        #print('client',client)
        client.copy_file(local_path, remote_path)
        #client.pool.join()
        #print('done')
        #client.disconnect()

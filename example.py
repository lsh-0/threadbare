from threadbare.state import settings
from threadbare.api import remote

def main():
    # it's embarassing how nice it is to play with global state...
    with settings(user='elife', host='34.201.187.7', quiet=False, discard_output=False) as env:
        #result = remote(r'echo -e "\e[31mRed Text\e[0m"', use_shell=False)
        #result = remote('echo "standard out"; echo "sleeping"; sleep 2; >&2 echo "standard error"; exit 2', combine_stderr=False)
        #result = remote_sudo('salt-call state.highstate')
        result = remote('foo=bar; echo "bar? $foo!"', use_shell=False)
        # read from stdin
        #result = remote('echo "> "; cat -')

        if env.get('quiet', False) and not env.get('discard_output', False):
            print('---')
            for line in result['stdout']:
                print('out:',line)

            for line in result['stderr']:
                print('err:',line)

        print('---')
        
        print('results:',result)
    
if __name__ == '__main__':
    main()

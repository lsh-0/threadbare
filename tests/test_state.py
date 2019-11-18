from functools import partial
from threadbare import state
from threadbare.state import settings

# 'local' state
# this *is not* what Fabric does

def test_env():
    "`settings` context manager will use the provided state dictionary if one is provided"
    env = {}
    assert len(env) == 0, "local env is not initially empty"
    assert len(state.ENV) == 0, "global env is not initially empty"
    with settings(env, foo='bar'):
        assert len(env) == 1, "env should contain one item"
        assert len(state.ENV) == 0, "global env should continue to be empty"
        assert env['foo'] == 'bar'
    assert len(env) == 0, "env is not finally empty"
    assert len(state.ENV) == 0, "global env should continue to be empty"

def test_nested_state():
    "settings decorator can be nested"
    env = {}
    with settings(env, foo='bar'):
        with settings(env, baz='bop'):
            assert env == {'foo': 'bar', 'baz': 'bop'}
    assert env == {}

def test_overridden_state():
    "overrides exist only for the scope of the context manager"
    env = {'foo': 'bar'}
    with settings(env, foo='baz'):
        assert env == {'foo': 'baz'}
    assert env == {'foo': 'bar'}

def test_deleted_state():
    "state is restored when a value is deleted"
    env = {'foo': 'bar'}
    with settings(env):
        del env['foo']
        assert env == {}
    assert env == {'foo': 'bar'}

def test_deleted_state_2():
    "state is restored when a value is removed"
    env = {'foo': 'bar'}
    with settings(env):
        env.pop('foo')
        assert env == {}
    assert env == {'foo': 'bar'}

def test_nested_state_initial_state():
    "state is returned to initial conditions"
    env = {'foo': 'bar'}
    with settings(env, baz='bop'):
        assert env == {'foo': 'bar', 'baz': 'bop'}
        with settings(env, boo='blah'):
            assert env == {'foo': 'bar', 'baz': 'bop', 'boo': 'blah'}
        assert env == {'foo': 'bar', 'baz': 'bop'}
    assert env == {'foo': 'bar'}

def test_uncontrolled_state_modification():
    "modifications to the state that happen outside of the context manager's control (with ... as ...) are reverted on exit"
    env = {'foo': {'bar': 'baz'}}
    with settings(env):
        env['foo']['bar'] = 'bop'
        assert env == {'foo': {'bar': 'bop'}}
    assert env == {'foo': {'bar': 'baz'}}

def test_settings_closure():
    "access to an enclosed state dictionary without reference to the original is still possible"
    enclosed = partial(settings, {})
    with enclosed(foo='bar') as env:
        assert env == {'foo': 'bar'}
        with enclosed(baz='bop') as env2:
            assert env2 == {'foo': 'bar', 'baz': 'bop'}
            assert env == {'foo': 'bar', 'baz': 'bop'}
        assert env == {'foo': 'bar'}

def test_settings_nested_closure():
    "state dictionary reference provided in nested scopes is preserved"
    env = {}
    enclosed = partial(settings, env)
    with enclosed(foo='bar'):
        with enclosed(baz='bop') as env2:
            assert env == env2 == {'foo':'bar','baz':'bop'}
    assert env == env2 == {}

# global state
# not pretty, often hard to reason about and may lead to weird behaviour if you're not careful.
# this is what Fabric does.

def test_global_env():
    "`settings` context manager uses global (and empty) state dictionary if a specific dictionary isn't supplied"
    assert state.ENV == {}
    with settings(foo='bar'):
        assert state.ENV == {'foo': 'bar'}
    assert state.ENV == {}

def test_global_nested_state():
    "context managers can be nested for global state"
    assert state.ENV == {}
    with settings(foo='bar'):
        with settings(baz='bop'):
            assert state.ENV == {'foo': 'bar', 'baz': 'bop'}
    assert state.ENV == {}

def test_global_overridden_state():
    "global overrides exist only for the scope of the context manager"
    assert state.ENV == {}
    with settings(foo='baz') as local_env:
        assert local_env == {'foo': 'baz'}
    # python vagary that this can still be referenced
    # it should still be as we expect though.
    assert local_env == {} 
    assert state.ENV == {}

def test_global_deleted_state():
    "original global state is restored if a value is deleted"
    assert state.ENV == {}
    with settings(foo='bar', bar='baz') as env:
        assert state.ENV == env == {'foo': 'bar', 'bar': 'baz'}
        del env['foo']
        assert state.ENV == env == {'bar': 'baz'}
    assert state.ENV == env == {}

def test_uncontrolled_global_state_modification():
    """modifications to global state that happen outside of the context manager's 
    control (with ... as ...) are available as expected BUT are reverted on exit"""
    assert state.ENV == {}
    with settings() as env:
        state.ENV['foo'] = {'bar': 'bop'}
        assert env == {'foo': {'bar': 'bop'}}
    assert state.ENV == env == {}
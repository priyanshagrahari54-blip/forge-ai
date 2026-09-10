"""Launch the Forge AI Cockpit with a demo project."""
import sys
from pathlib import Path

import uvicorn

from forge.api.app import create_app
from forge.control.control_plane import ControlPlane, ControlConfig
from forge.security.policy import PermissionPolicy, PermissionRule, Resource

# Set up demo project
proj = Path('/tmp/forge-demo-project')
proj.mkdir(exist_ok=True)
(proj / 'app.py').write_text('''def hello():
    """Return a greeting."""
    return "Hello World"

def add(a, b):
    """Add two numbers."""
    return a + b
''')
(proj / 'test_app.py').write_text('''from app import hello, add

def test_hello():
    assert hello() == "Hello World"

def test_add():
    assert add(2, 3) == 5
''')
(proj / 'README.md').write_text('# Demo Project\nA sample project for Forge AI.\n')
(proj / '.git').mkdir(exist_ok=True)

# Full permissions for demo.
#
# r1 allows exactly the repository test command Forge runs
# ([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]).
# The scope is matched against the executable basename and the args must
# match exactly, so targeted runs (extra test paths appended) and every
# other command still fall through to the normal approval flow.
_test_executable = Path(sys.executable).name
_test_args = ("-B", "-m", "pytest", "-q", "-p", "no:cacheprovider")
policy = PermissionPolicy(rules=[
    PermissionRule(id='r1', resource=Resource.TERMINAL, operation='execute',
                   scope=_test_executable, args=_test_args, effect='ALLOW'),
    PermissionRule(id='r2', resource=Resource.MODEL, operation='call', scope='', effect='ALLOW'),
    PermissionRule(id='r3', resource=Resource.VISION, operation='analyze', scope='image', effect='ALLOW'),
    PermissionRule(id='r4', resource=Resource.VOICE, operation='command', scope='', effect='ALLOW'),
    PermissionRule(id='r5', resource=Resource.AGENT, operation='execute', scope='', effect='ALLOW'),
    PermissionRule(id='r6', resource=Resource.MEMORY, operation='write', scope='', effect='ALLOW'),
    PermissionRule(id='r7', resource=Resource.MEMORY, operation='read', scope='', effect='ALLOW'),
    PermissionRule(id='r8', resource=Resource.VISION, operation='execute', scope='', effect='ALLOW'),
])

config = ControlConfig(
    db_path='/tmp/forge-demo-db/db.sqlite',
    projects={'demo': str(proj)},
    policy=policy,
)
plane = ControlPlane(config)
plane.start()

app = create_app(plane)
# Local-dev auth has no passwords: never bind beyond loopback here.
print("Starting Forge AI Cockpit on http://127.0.0.1:8080")
print("Demo project: /tmp/forge-demo-project")
uvicorn.run(app, host='127.0.0.1', port=8080)

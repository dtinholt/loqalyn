"""Product identity and non-destructive compatibility with earlier alphas."""
import os
from pathlib import Path


def setting(name, default=None, *, environ=None):
    """Prefer the new environment name, including an explicitly empty value."""
    env = os.environ if environ is None else environ
    return env.get('LOQALYN_' + name, env.get('SWITCHYARD_' + name, default))


def profile_directory(*, native=False, home=None, environ=None):
    """Reuse an existing profile in place; never copy credentials or reset policy."""
    override = setting('DIR', environ=environ)
    if override is not None:
        if not override.strip():
            raise ValueError('The data directory cannot be empty.')
        return Path(override).expanduser()
    root = Path.home() if home is None else Path(home)
    if native:
        root = root / 'Library' / 'Application Support'
        current, legacy = root / 'Loqalyn', root / 'Switchyard'
    else:
        current, legacy = root / '.loqalyn', root / '.switchyard'
    # lexists preserves fail-closed validation for broken links and unsafe paths.
    # The normal private-directory checks still validate the chosen path.
    if os.path.lexists(current):
        return current
    if os.path.lexists(legacy):
        return legacy
    return current


def keychain_password(keyring, provider):
    """Read old credentials only if the new namespace has no saved credential."""
    value = keyring.get_password('loqalyn', provider)
    return value if value is not None else keyring.get_password('switchyard', provider)

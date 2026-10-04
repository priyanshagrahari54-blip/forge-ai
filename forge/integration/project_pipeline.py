"""Project profile loading facade."""
def load(root,loader):
    return loader(root) if callable(loader) else None

"""Assemble the common context passed between Forge stages."""
def assemble(task=None,contract=None,plan=None,project=None):
    return {"task":task,"contract":contract,"plan":plan,"project":project}

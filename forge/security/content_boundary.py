"""External content is data, never authority."""
def mark_untrusted(content,source="external"):
    return {"content":content,"source":source,"trusted":False}

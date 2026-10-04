"""Plugin adoption facade using existing manifest validation."""
def validate(manifest,validator):
    return validator(manifest) if callable(validator) else False

"""
Validation utilities

Common data validation functions.
"""


def validate_suppression_type(suppression_type: str) -> bool:
    """Validates a suppression type"""
    valid_types = ["bounce", "complaint", "unsubscribe", "manual"]
    return suppression_type.lower() in valid_types

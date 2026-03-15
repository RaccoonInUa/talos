class ContractViolationError(Exception):
    """
    Raised when incoming data fails validation at the system boundary.
    This strictly indicates malformed external input, not an internal logic error.
    
    Rule: Validation errors at system boundaries MUST be wrapped in ContractViolationError.
    """
    pass
"""Build HLASM source with the fields in the columns the assembler reads them from.

Tests that write assembler as free text prove nothing about a column language - the whole
first stage of this tool is the column rules - so every fixture goes through here.
"""


def asm(name="", op="", operand="", cont=False, seq=""):
    """One physical line: name at column 1, operation at 10, operand at 16.

    The operation must be followed by at least one blank, so an operation longer than five
    characters pushes the operand past column 16 rather than running into it. A line with
    no continuation and no sequence field is left its natural length, so a test can widen
    the margins with ICTL and see more of it.
    """
    head = "{0}{1}".format(name.ljust(9), op)
    line = "{0} {1}".format(head, operand) if len(head) >= 15 else \
        "{0}{1}".format(head.ljust(15), operand)
    if not cont and not seq:
        return line
    return line.ljust(71)[:71] + ("X" if cont else " ") + seq


def cont(operand, more=False, seq=""):
    """A continuation line: operand at the continue column, 16."""
    return (" " * 15 + operand).ljust(71)[:71] + ("X" if more else " ") + seq


def module(*rows):
    """Rows given as ``(name, op, operand)`` tuples or as raw strings."""
    out = []
    for row in rows:
        out.append(asm(*row) if isinstance(row, tuple) else row)
    return "\n".join(out)

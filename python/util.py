def container_to_str(s):
    l_br = "{ "
    r_br = " }"
    if isinstance(s, list) or isinstance(s, tuple):
        l_br = "[ "
        r_br = " ]"

    # Provide stable order for sets.
    is_set_type = isinstance(s, set) or isinstance(s, frozenset)
    return l_br + ", ".join([str(c) for c in (sorted(s) if is_set_type else s)]) + r_br


def rnd(x):
    return round(x, 2)

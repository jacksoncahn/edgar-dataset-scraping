FTC = [{"start": "164.62.0.0", "end": "164.62.255.255"}]
DOJ = [
    {"start": "149.101.0.0", "end": "149.101.255.255"},
    {"start": "192.58.200.0", "end": "192.58.203.255"},
    {"start": "63.145.235.0", "end": "63.145.235.255"},
]

# 149.101.0.0 - 149.101.255.255
# 192.58.200.0 - 192.58.203.255
# 63.145.235.176 - 63.145.235.183 -> becomes 63.145.235.0 - 63.145.235.255 as we are unable to differentiate between those last 3 digits
#
# EDGAR anonymizes the last octet (e.g. 149.101.1.cdg), so matching uses only
# the first three octets (a.b.c.*).


def _ip_to_int(ip_address):
    """
    Convert an IP to an int using only the first three octets.

    The 4th octet may be numeric or EDGAR's letter anonymization; it is ignored.
    """
    octets = ip_address.split(".")
    if len(octets) != 4:
        raise ValueError(f"Invalid IP address: {ip_address}")

    numbers = [int(octets[0]), int(octets[1]), int(octets[2])]
    if any(n < 0 or n > 255 for n in numbers):
        raise ValueError(f"Invalid IP address: {ip_address}")

    return (numbers[0] << 24) | (numbers[1] << 16) | (numbers[2] << 8)


def ip_range(ip_address):
    """
    Return "ftc" / "doj" / "doj_uncertain" for target ranges, else None.

    Malformed IPs (not 4 parts, or non-numeric first three octets) return None
    and do not raise, so filtering can skip them without aborting.
    """
    try:
        ip_value = _ip_to_int(ip_address)
    except (TypeError, ValueError):
        return None

    for block in FTC:
        if _ip_to_int(block["start"]) <= ip_value <= _ip_to_int(block["end"]):
            return "ftc"

    for i in range(len(DOJ)):
        if _ip_to_int(DOJ[i]["start"]) <= ip_value <= _ip_to_int(DOJ[i]["end"]):
            if i == len(DOJ) - 1:
                return "doj_uncertain"
            return "doj"

    return None

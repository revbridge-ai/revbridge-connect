"""IP-warmup fractional router (the "divisor").

Pushes a controlled, SPF-safe fraction of the eligible traffic through pool IPs
in warmup, to warm them up with real volume. Complements (does not conflict
with) the poolset controller (which REMOVES a toxic IP) and recipient_failover.
"""

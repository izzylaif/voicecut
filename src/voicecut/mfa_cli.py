"""Start the Montreal Forced Aligner's command line.

VoiceCut runs this file with the Python of MFA's own environment
(``micromamba run -p <prefix> python mfa_cli.py align_hf ...``) instead of the
``mfa`` command: on Windows that command is a launcher that cannot start
Python from a folder whose path has a space in it (``C:\\Users\\First Last``).
It runs in MFA's environment, so it must not import VoiceCut.  The guard keeps
MFA's worker processes, which import this file again, from starting MFA.
"""

if __name__ == "__main__":
    from montreal_forced_aligner.command_line.mfa import mfa_cli

    mfa_cli()

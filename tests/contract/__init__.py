"""The restore contract: what every reader must do, stated once as data.

``restore_contract.py`` holds the cases and the drivers that run the real
readers on them (#886).  The test that runs the table is
``tests/recovery_hardening/test_contract_readers.py``.

``script_contract.py`` holds the second table, ``SCRIPT_CASES``: what
restore.sh and restore.bat make of what a reader did (#887).  The test
that runs it is ``tests/recovery_hardening/test_restore_script_outcomes.py``.
``exit_codes.py`` names the readers' and the scripts' exit codes, with
the key words their rows in the docs' exit-code tables must hold;
``doc_tables.py`` reads those tables (#888), and
``tests/recovery_hardening/test_doc_exit_tables.py`` checks them.
"""

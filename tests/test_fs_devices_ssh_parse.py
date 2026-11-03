"""SSH connection string parsing — accept familiar forms, reject shell syntax."""

import pytest

from src.fs_devices.ssh_parse import SshParseError, parse_ssh_connection


def test_parse_user_host():
    parsed = parse_ssh_connection("ssh tovakai@100.64.1.2")
    assert parsed.username == "tovakai"
    assert parsed.hostname == "100.64.1.2"
    assert parsed.port == 22
    assert parsed.alias is None


def test_parse_alias():
    parsed = parse_ssh_connection("ssh zenbook")
    assert parsed.alias == "zenbook"
    assert parsed.hostname == "zenbook"
    assert parsed.username is None


def test_parse_port_and_identity():
    parsed = parse_ssh_connection("ssh -p 2222 -i ~/.ssh/id_ed25519 user@host.example")
    assert parsed.port == 2222
    assert parsed.identity_file == "~/.ssh/id_ed25519"
    assert parsed.username == "user"
    assert parsed.hostname == "host.example"


def test_parse_identityfile_option():
    parsed = parse_ssh_connection("ssh -o IdentityFile=~/.ssh/id_rsa box")
    assert parsed.alias == "box"
    assert parsed.identity_file == "~/.ssh/id_rsa"


def test_reject_pipe():
    with pytest.raises(SshParseError):
        parse_ssh_connection("ssh user@host | cat")


def test_reject_command_substitution():
    with pytest.raises(SshParseError):
        parse_ssh_connection("ssh user@$(hostname)")


def test_reject_unsupported_flag():
    with pytest.raises(SshParseError):
        parse_ssh_connection("ssh -L 8080:localhost:80 user@host")


def test_reject_unsupported_o_option():
    with pytest.raises(SshParseError):
        parse_ssh_connection("ssh -o StrictHostKeyChecking=no user@host")


def test_magicdns_hostname():
    parsed = parse_ssh_connection("user@zenbook.tailnet-name.ts.net")
    assert parsed.hostname == "zenbook.tailnet-name.ts.net"
    assert parsed.username == "user"

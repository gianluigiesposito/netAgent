# tests/test_execute_wait.py
import sys
import asyncio
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import RouterCommands, CommandPair
from nodes.execute import _execute_device

@pytest.mark.asyncio
async def test_execute_device_router_wait():
    """
    Verifica che sui router L3 (es. cisco_ios) la procedura di convergenza
    esegua un'attesa fissa di 30 secondi.
    """
    router_name = "R1"
    commands_obj = RouterCommands(pairs=[
        CommandPair(
            cmd="interface Ethernet0/0\nno shutdown",
            rollback="interface Ethernet0/0\nshutdown"
        )
    ])
    reachability = {"R1": "REACHABLE"}
    inventory = {
        "R1": {
            "host": "127.0.0.1",
            "port": 5011,
            "vendor": "cisco_ios",
            "connection_type": "cisco_telnet",
        }
    }
    semaphore = asyncio.Semaphore(1)

    mock_conn = AsyncMock()
    mock_conn.send_command.return_value = "R1(config-if)#"
    mock_conn.save_config.return_value = True

    mock_get_conn = MagicMock()
    mock_get_conn.__aenter__.return_value = mock_conn

    with patch("nodes.execute.get_connection", return_value=mock_get_conn), \
         patch("nodes.execute.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        
        msg, success = await _execute_device(
            router_name, commands_obj, reachability, inventory, semaphore
        )

        assert success is True
        assert "SUCCESS" in msg
        mock_sleep.assert_any_call(30.0)


@pytest.mark.asyncio
async def test_execute_device_switch_wait_fwd_immediately():
    """
    Verifica che sugli switch L2 (es. cisco_switch) la procedura interroghi lo
    stato dello Spanning Tree (STP) e termini subito senza attendere se tutte
    le VLAN attive sono già in stato Forwarding (FWD).
    """
    router_name = "SW1"
    commands_obj = RouterCommands(pairs=[
        CommandPair(
            cmd="interface Ethernet0/0\nno shutdown",
            rollback="interface Ethernet0/0\nshutdown"
        )
    ])
    reachability = {"SW1": "REACHABLE"}
    inventory = {
        "SW1": {
            "host": "127.0.0.1",
            "port": 5013,
            "vendor": "cisco_switch",
            "connection_type": "cisco_telnet",
        }
    }
    semaphore = asyncio.Semaphore(1)

    stp_fwd_output = """
Vlan                Role Sts Cost      Prio.Nbr Type
------------------- ---- --- --------- -------- --------------------------------
VLAN0010            Desg FWD 100       128.1    Shr Edge 
VLAN0020            Desg FWD 100       128.1    Shr Edge 
"""

    mock_conn = AsyncMock()
    async def mock_send(cmd):
        if "spanning-tree" in cmd:
            return stp_fwd_output
        return "SW1(config-if)#"
    mock_conn.send_command.side_effect = mock_send
    mock_conn.save_config.return_value = True

    mock_get_conn = MagicMock()
    mock_get_conn.__aenter__.return_value = mock_conn

    with patch("nodes.execute.get_connection", return_value=mock_get_conn), \
         patch("nodes.execute.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        
        msg, success = await _execute_device(
            router_name, commands_obj, reachability, inventory, semaphore
        )

        assert success is True
        assert "SUCCESS" in msg
        # Verifichiamo che non sia stata eseguita alcuna attesa di convergenza (5s, 15s, 30s)
        for delay in [5.0, 15.0, 30.0]:
            assert call(delay) not in mock_sleep.call_args_list


@pytest.mark.asyncio
async def test_execute_device_switch_wait_fwd_after_retry():
    """
    Verifica che sugli switch L2, se lo stato STP iniziale di una VLAN è in transizione
    (es. LIS o LRN), il ciclo esegua il polling sequenziale (5s, poi 15s, poi 30s)
    e termini con successo non appena le VLAN passano a FWD.
    """
    router_name = "SW1"
    commands_obj = RouterCommands(pairs=[
        CommandPair(
            cmd="interface Ethernet0/0\nno shutdown",
            rollback="interface Ethernet0/0\nshutdown"
        )
    ])
    reachability = {"SW1": "REACHABLE"}
    inventory = {
        "SW1": {
            "host": "127.0.0.1",
            "port": 5013,
            "vendor": "cisco_switch",
            "connection_type": "cisco_telnet",
        }
    }
    semaphore = asyncio.Semaphore(1)

    stp_lis_output = """
Vlan                Role Sts Cost      Prio.Nbr Type
------------------- ---- --- --------- -------- --------------------------------
VLAN0010            Desg LIS 100       128.1    Shr Edge 
"""

    stp_fwd_output = """
Vlan                Role Sts Cost      Prio.Nbr Type
------------------- ---- --- --------- -------- --------------------------------
VLAN0010            Desg FWD 100       128.1    Shr Edge 
"""

    mock_conn = AsyncMock()
    call_count = 0

    async def mock_send(cmd):
        nonlocal call_count
        if "spanning-tree" in cmd:
            call_count += 1
            if call_count == 1:
                return stp_lis_output
            else:
                return stp_fwd_output
        return "SW1(config-if)#"
        
    mock_conn.send_command.side_effect = mock_send
    mock_conn.save_config.return_value = True

    mock_get_conn = MagicMock()
    mock_get_conn.__aenter__.return_value = mock_conn

    with patch("nodes.execute.get_connection", return_value=mock_get_conn), \
         patch("nodes.execute.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        
        msg, success = await _execute_device(
            router_name, commands_obj, reachability, inventory, semaphore
        )

        assert success is True
        assert "SUCCESS" in msg
        # Dovrebbe essere stato eseguito un solo sleep di 5.0s per la convergenza
        assert call(5.0) in mock_sleep.call_args_list
        # Non dovrebbero esserci stati altri sleep di convergenza successivi (15.0s, 30.0s)
        assert call(15.0) not in mock_sleep.call_args_list
        assert call(30.0) not in mock_sleep.call_args_list


@pytest.mark.asyncio
async def test_execute_device_switch_skips_svi():
    """
    Verifica che sugli switch L2 le interfacce logiche/L3 come Vlan99, sottointerfacce
    o Loopback vengano escluse dal controllo di Spanning Tree.
    """
    router_name = "SW1"
    commands_obj = RouterCommands(pairs=[
        CommandPair(
            cmd="interface Vlan99\nno shutdown\ninterface Ethernet0/0.10\nno shutdown",
            rollback="interface Vlan99\nshutdown\ninterface Ethernet0/0.10\nshutdown"
        )
    ])
    reachability = {"SW1": "REACHABLE"}
    inventory = {
        "SW1": {
            "host": "127.0.0.1",
            "port": 5013,
            "vendor": "cisco_switch",
            "connection_type": "cisco_telnet",
        }
    }
    semaphore = asyncio.Semaphore(1)

    mock_conn = AsyncMock()
    mock_conn.send_command.return_value = "SW1(config-if)#"
    mock_conn.save_config.return_value = True

    mock_get_conn = MagicMock()
    mock_get_conn.__aenter__.return_value = mock_conn

    with patch("nodes.execute.get_connection", return_value=mock_get_conn), \
         patch("nodes.execute.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        
        msg, success = await _execute_device(
            router_name, commands_obj, reachability, inventory, semaphore
        )

        assert success is True
        assert "SUCCESS" in msg
        # Verifichiamo che non sia stato eseguito alcun comando show spanning-tree (poiché Vlan99 ed Ethernet0/0.10 sono ignorate)
        for call_arg in mock_conn.send_command.call_args_list:
            assert "spanning-tree" not in call_arg[0][0]
        # Verifichiamo che non sia stata eseguita alcuna attesa di convergenza (5s, 15s, 30s)
        for delay in [5.0, 15.0, 30.0]:
            assert call(delay) not in mock_sleep.call_args_list


def test_is_cli_error_status_bypass():
    from nodes.execute import _is_cli_error
    
    # Standard Cisco status messages should NOT be considered errors
    assert not _is_cli_error("% Generating 2048 bit RSA keys, keys will be non-exportable...\n[OK] (elapsed time was 0 seconds)")
    assert not _is_cli_error("% SSH-2.0-OpenSSH_8.2p1 Ubuntu-4ubuntu0.5 has been enabled")
    assert not _is_cli_error("% SSH 2.0 has been enabled")
    assert not _is_cli_error("% Note: SSH will be disabled until a domain name is configured")
    
    # Real errors should still be errors
    assert _is_cli_error("% Invalid input detected at '^' marker.")
    assert _is_cli_error("% Incomplete command.")
    assert _is_cli_error("% Ambiguous command.")



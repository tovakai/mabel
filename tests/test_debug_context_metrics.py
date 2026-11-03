from src.agent_loop import _compute_final_metrics, _last_request_occupancy


def test_context_uses_last_request_while_usage_remains_cumulative(monkeypatch):
    from src import agent_loop
    monkeypatch.setattr(agent_loop, 'estimate_tokens', lambda messages: 12000)
    messages=[{'role':'user','content':'current synthetic request'}]
    count,source=_last_request_occupancy(messages,19562,True)
    metrics=_compute_final_metrics(messages,'answer',10,1,32768,233650,1200,True,[],[],
                                   request_context_tokens=count)
    assert source=='provider_last_request'
    assert metrics['input_tokens']==233650 and metrics['request_context_tokens']==19562
    assert metrics['context_percent']==59.7
    count,source=_last_request_occupancy(messages,19562,False)
    assert count==12000 and source=='estimated_messages_only'

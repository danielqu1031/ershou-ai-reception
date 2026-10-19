def test_stop_while_http_down_blocks_future_restore(tmp_path,monkeypatch):
    import manage
    agent=tmp_path/'agent.plist'
    agent.touch()
    calls=[]
    monkeypatch.setattr(manage,'DATA',tmp_path)
    monkeypatch.setattr(manage,'AGENT',agent)
    monkeypatch.setattr(manage,'reachable',lambda _:False)
    monkeypatch.setattr(manage,'launchctl',lambda *args:calls.append(args) or 0)
    manage.stop()
    assert (tmp_path/'recovery.disabled').exists()
    assert calls[0][0] == 'disable'
    assert calls[1][0] == 'bootout'
    assert all(call[-1].endswith('/com.ershoureception.opensource') for call in calls)

"""Authenticated configuration and durable code-sync requests; no raw command API."""
from flask import jsonify,request
from code_sync import CodeSync
from sync_runtime import SyncError,require


def register_code_sync(app,state,audit):
    hub=CodeSync(state,operations=bool(app.config.get('NATIVE_MODE') and app.config.get('NATIVE_OPERATIONS_ENABLED')))
    app.extensions['code_sync']=hub

    def failed(error):return jsonify(error=str(error) if isinstance(error,SyncError) else 'XD_SYNC_STATE_UNAVAILABLE'),409

    @app.get('/api/code-sync')
    def sync_listing():
        try:return jsonify(hub.listing())
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

    @app.post('/api/code-sync/<kind>')
    def sync_save(kind):
        request.max_content_length=65536
        try:
            result=hub.save_credential(request.get_json()) if kind=='credentials' else hub.save(kind,request.get_json())
            audit('code-sync-save',kind+':'+result['id'],'saved');return jsonify(result=result)
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

    @app.post('/api/code-sync/sources/<identifier>/preview')
    def sync_preview(identifier):
        try:require(request.get_json()=={},'XD_SYNC_INPUT_INVALID');return jsonify(result=hub.preview(identifier))
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

    @app.post('/api/code-sync/devices/<identifier>/check')
    def sync_device_check(identifier):
        try:require(request.get_json()=={},'XD_SYNC_INPUT_INVALID');return jsonify(result=hub.check_device(identifier))
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

    @app.post('/api/code-sync/targets/<identifier>/check')
    def sync_target_check(identifier):
        try:
            require(request.get_json()=={},'XD_SYNC_INPUT_INVALID');target=hub.store.get('targets',identifier)
            require(target is not None,'XD_SYNC_TARGET_NOT_FOUND');return jsonify(result=hub.inspect_target(target))
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

    @app.post('/api/code-sync/run')
    def sync_run():
        try:
            job=hub.enqueue(request.get_json());audit('code-sync-run',job['source'],'queued');return jsonify(job=job),202
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

    @app.post('/api/code-sync/jobs/<identifier>/reconcile')
    def sync_reconcile(identifier):
        try:
            require(request.get_json()=={},'XD_SYNC_INPUT_INVALID');require(hub.operations and hub.policy()['execution'],'XD_SYNC_EXECUTION_DISABLED')
            hub.store.request_reconcile(identifier);audit('code-sync-reconcile',identifier,'queued');return jsonify(queued=True),202
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

    @app.post('/api/code-sync/jobs/<identifier>/close')
    def sync_close(identifier):
        try:
            hub.close_job(identifier,request.get_json());audit('code-sync-close',identifier,'closed-unconfirmed');return jsonify(closed=True,verified=False)
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

    @app.post('/api/code-sync/archive-history')
    def sync_archive():
        try:
            require(request.get_json()=={},'XD_SYNC_INPUT_INVALID');result=hub.archive();audit('code-sync-archive','history','archived');return jsonify(result=result)
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)


    @app.get('/api/code-sync/archives/<name>')
    def sync_archive_read(name):
        try:return jsonify(result=hub.read_archive(name))
        except (OSError,ValueError,TypeError,KeyError) as error:return failed(error)

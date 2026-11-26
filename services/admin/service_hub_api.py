"""Authenticated integration of independently started applications."""
from flask import jsonify, request

from native_services import ServiceHub, ServiceError, probe_endpoint
from gateway_runtime import GatewayRuntime
from registry_store import RegistryError
from service_binding_matches import binding_preview, grant_project_read, project_services, read_host


def register_service_hub(app,state,audit,redactor,domains):
    hub=ServiceHub(state,adapter=app.config.get('SERVICE_HUB_ADAPTER'),probe=app.config.get('SERVICE_HUB_PROBE') or probe_endpoint,
        domains=domains,monitors=lambda:{r['id'] for r in app.extensions['resource_monitor'].listing()['apps']})
    gateway=GatewayRuntime(hub,**(app.config.get('SERVICE_HUB_GATEWAY_ADAPTER') or {}))
    app.extensions['service_hub']=hub;app.extensions['native_gateway']=gateway

    def failure(exc):return jsonify(error=str(exc) if isinstance(exc,(ServiceError,RegistryError)) else 'XD_SERVICE_STATE_UNAVAILABLE'),409

    @app.get('/api/service-hub')
    def hub_listing():
        try:
            value=hub.listing();value['native']=bool(app.config.get('NATIVE_MODE'))
            value['operations_enabled']=bool(app.config.get('NATIVE_MODE') and app.config.get('NATIVE_OPERATIONS_ENABLED'))
            if request.args.get('project_services') == '1':
                runner = app.config.get('SERVICE_HUB_MATCH_RUNNER', read_host) if value['native'] else None
                value['project_services'] = project_services(value, runner)
            try:
                preview=gateway.preview();value['gateway']={k:v for k,v in preview.items() if k!='configuration'}
            except (OSError,ValueError) as exc:value['gateway']={'error':str(exc) if isinstance(exc,(ServiceError,RegistryError)) else 'XD_GATEWAY_PREVIEW_UNAVAILABLE'}
            return jsonify(value)
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.post('/api/service-hub/projects')
    def hub_save():
        try:
            row=hub.save(request.get_json());audit('service-register',row['id'],'success')
            return jsonify(project=row,runtime_started=False)
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.get('/api/service-hub/bindings/preview')
    def hub_binding_preview():
        try:
            runner = app.config.get('SERVICE_HUB_MATCH_RUNNER', read_host) if app.config.get('NATIVE_MODE') else None
            return jsonify(binding_preview(hub.listing(), runner))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return failure(exc)

    @app.post('/api/service-hub/projects/<identifier>/read-grants')
    def hub_grant_read(identifier):
        try:
            if not (app.config.get('NATIVE_MODE') and app.config.get('NATIVE_OPERATIONS_ENABLED')):
                raise ServiceError('XD_SERVICE_OPERATIONS_DISABLED')
            grant = grant_project_read(hub, identifier, request.get_json(), app.config.get('SERVICE_HUB_MATCH_RUNNER', read_host))
            audit('service-read-grant', identifier + '/' + grant['scope'] + '/' + grant['unit'], 'success')
            return jsonify(grant=grant), 201
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return failure(exc)

    @app.post('/api/service-hub/bindings')
    def hub_bind_services():
        try:
            body=request.get_json()
            result=hub.bind_services(body)
            targets=', '.join(entry['project']+'/'+entry['alias'] for entry in body['bindings'])
            audit('service-bind',targets,'success')
            return jsonify(result)
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.post('/api/service-hub/projects/<identifier>/remove')
    def hub_remove(identifier):
        try:
            hub.remove(identifier,request.get_json());audit('service-unregister',identifier,'success')
            return jsonify(removed=True,gateway_apply_required=True,runtime_stopped=False)
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.post('/api/service-hub/projects/<identifier>/health')
    def hub_health(identifier):
        try:return jsonify(result=hub.health(identifier))
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.get('/api/service-hub/projects/<identifier>/status')
    @app.get('/api/service-hub/projects/<identifier>/services/<alias>/status')
    def hub_status(identifier,alias=None):
        try:return jsonify(result=hub.status(identifier,alias))
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.get('/api/service-hub/projects/<identifier>/logs')
    @app.get('/api/service-hub/projects/<identifier>/services/<alias>/logs')
    def hub_logs(identifier,alias=None):
        try:
            row,policy=hub.bound(identifier,alias)
            if row['service'] is None:raise ServiceError('XD_SERVICE_NOT_SUPERVISED')
            value=hub.adapter.logs(policy['services'][row['service']]);value['text']=redactor(value['text'])
            return jsonify(value)
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.post('/api/service-hub/projects/<identifier>/action')
    @app.post('/api/service-hub/projects/<identifier>/services/<alias>/action')
    def hub_action(identifier,alias=None):
        try:
            if not app.config.get('NATIVE_MODE') or not app.config.get('NATIVE_OPERATIONS_ENABLED'):raise ServiceError('XD_SERVICE_OPERATIONS_DISABLED')
            value=hub.action(identifier,request.get_json(),alias);audit('native-'+value['action'],identifier,value['state'])
            return jsonify(operation=value)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            audit('native-operation',identifier,'unconfirmed')
            return failure(exc)

    @app.get('/api/service-hub/gateway/preview')
    def hub_preview():
        try:return jsonify(gateway.preview())
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.post('/api/service-hub/gateway/apply')
    def hub_apply():
        try:
            if not app.config.get('NATIVE_MODE') or not app.config.get('NATIVE_OPERATIONS_ENABLED'):raise ServiceError('XD_SERVICE_OPERATIONS_DISABLED')
            value=gateway.apply(request.get_json());audit('native-gateway','xdocker-gateway',value['state'])
            return jsonify(result=value)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            audit('native-gateway','xdocker-gateway','unconfirmed')
            return failure(exc)

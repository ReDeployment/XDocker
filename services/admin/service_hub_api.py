"""Authenticated integration of independently started applications."""
from flask import jsonify, request

from native_services import ServiceHub, ServiceError, probe_endpoint
from gateway_runtime import GatewayRuntime
from registry_store import RegistryError


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
    def hub_status(identifier):
        try:return jsonify(result=hub.status(identifier))
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.get('/api/service-hub/projects/<identifier>/logs')
    def hub_logs(identifier):
        try:
            row,policy=hub.bound(identifier)
            if row['service'] is None:raise ServiceError('XD_SERVICE_NOT_SUPERVISED')
            value=hub.adapter.logs(policy['services'][row['service']]);value['text']=redactor(value['text'])
            return jsonify(value)
        except (OSError,ValueError,KeyError,TypeError) as exc:return failure(exc)

    @app.post('/api/service-hub/projects/<identifier>/action')
    def hub_action(identifier):
        try:
            if not app.config.get('NATIVE_MODE') or not app.config.get('NATIVE_OPERATIONS_ENABLED'):raise ServiceError('XD_SERVICE_OPERATIONS_DISABLED')
            value=hub.action(identifier,request.get_json());audit('native-'+value['action'],identifier,value['state'])
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

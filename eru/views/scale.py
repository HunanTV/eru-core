# coding: utf-8

from flask import Blueprint

from eru.models import App
from eru.common import code
from eru.utils.views import jsonify, EruAbortException


bp = Blueprint('scale', __name__, url_prefix='/api/scale')


@bp.route('/<name>/<version>/info')
@jsonify()
def touch_version_scale_info(name, version):
    app = App.get_by_name(name)
    if not app:
        raise EruAbortException(code.HTTP_NOT_FOUND, 'App %s not found' % name)
    v = app.get_version(version)
    if not v:
        raise EruAbortException(code.HTTP_NOT_FOUND, 'Version %s not found' % version)
    cs = v.containers.limit(1).all()
    if not cs:
        raise EruAbortException(code.HTTP_NOT_FOUND, 'Not deployed')
    c = cs[0]
    return {'group': c.host.group.name, 'pod': c.host.pod.name, 'ncore': len(c.cores.all())}


@bp.errorhandler(EruAbortException)
@jsonify()
def eru_abort_handler(exception):
    return {'r': 1, 'msg': exception.msg, 'status_code': exception.code}

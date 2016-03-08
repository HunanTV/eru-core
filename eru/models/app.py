# coding:utf-8

import sqlalchemy.exc
from datetime import datetime
from werkzeug.utils import cached_property
from sqlalchemy import event
from sqlalchemy import DDL

from eru.clients import rds
from eru.models import db
from eru.models.base import Base
from eru.models.image import Image
from eru.models.appconfig import AppConfig, ResourceConfig


class Version(Base):
    __tablename__ = 'version'

    sha = db.Column(db.CHAR(40), index=True, nullable=False)
    app_id = db.Column(db.Integer, db.ForeignKey('app.id'))
    created = db.Column(db.DateTime, default=datetime.now)

    containers = db.relationship('Container', backref='version', lazy='dynamic')
    tasks = db.relationship('Task', backref='version', lazy='dynamic')

    def __init__(self, sha, app_id):
        self.sha = sha
        self.app_id = app_id

    @classmethod
    def create(cls, sha, app_id):
        try:
            version = cls(sha, app_id)
            db.session.add(version)
            db.session.commit()
            return version
        except sqlalchemy.exc.IntegrityError:
            db.session.rollback()
            return None

    @classmethod
    def get_by_app_and_version(cls, application, sha):
        return cls.query.filter(cls.sha.like('{}%'.format(sha)), cls.app_id == application.id).one()

    @property
    def name(self):
        return self.app.name

    @cached_property
    def appconfig(self):
        return AppConfig.get_by_name_and_version(self.name, self.short_sha)

    @property
    def short_sha(self):
        return self.sha[:7]

    @property
    def user_id(self):
        return self.app.user_id

    def _get_falcon(self):
        return rds.smembers('eru:falcon:version:%s:expression' % self.id) or set()
    def _set_falcon(self, expr_ids):
        rds.delete('eru:falcon:version:%s:expression' % self.id)
        for i in expr_ids:
            rds.sadd('eru:falcon:version:%s:expression' % self.id, i)
    def _del_falcon(self):
        rds.delete('eru:falcon:version:%s:expression' % self.id)
    falcon_expression_ids = property(_get_falcon, _set_falcon, _del_falcon)
    del _get_falcon, _set_falcon, _del_falcon

    def list_containers(self, start=0, limit=20):
        from .container import Container
        q = self.containers.order_by(Container.id.desc()).offset(start)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def list_tasks(self, start=0, limit=20):
        from .task import Task
        q = self.tasks.order_by(Task.id.desc()).offset(start)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def get_resource_config(self, env='prod'):
        return ResourceConfig.get_by_name_and_env(self.name, env)

    def get_ports(self, entrypoint):
        entry = self.appconfig.entrypoints.get(entrypoint, {})
        ports = entry.get('ports', [])
        return [int(p.split('/')[0]) for p in ports]

    def get_image(self):
        return Image.get_by_app_and_version(self.app_id, self.id)

    def to_dict(self):
        d = super(Version, self).to_dict()
        d['name'] = self.name
        d['appconfig'] = self.appconfig.to_dict()
        d['image'] = self.get_image()
        return d


class App(Base):
    __tablename__ = 'app'

    name = db.Column(db.CHAR(32), nullable=False, unique=True)
    git = db.Column(db.String(255), nullable=False)
    update = db.Column(db.DateTime, default=datetime.now)
    _user_id = db.Column(db.Integer, nullable=False, default=0)

    versions = db.relationship('Version', backref='app', lazy='dynamic')
    containers = db.relationship('Container', backref='app', lazy='dynamic')
    tasks = db.relationship('Task', backref='app', lazy='dynamic')

    def __init__(self, name, git):
        self.name = name
        self.git = git

    @classmethod
    def get_or_create(cls, name, git):
        app = cls.query.filter(cls.name == name).first()
        if app:
            return app
        try:
            app = cls(name, git)
            db.session.add(app)
            db.session.commit()
            return app
        except sqlalchemy.exc.IntegrityError:
            db.session.rollback()
            return None

    @classmethod
    def get_by_name(cls, name):
        return cls.query.filter(cls.name == name).first()

    @classmethod
    def list_all(cls, start=0, limit=20):
        q = cls.query.order_by(cls.name.asc())
        return q[start:start+limit]

    @property
    def user_id(self):
        """默认使用id, 如果不对可以通过_user_id手动纠正."""
        return self._user_id or self.id

    def get_version(self, version):
        return self.versions.filter(Version.sha.like('{}%'.format(version))).first()

    def get_resource_config(self, env='prod'):
        return ResourceConfig.get_by_name_and_env(self.name, env)

    def list_resource_config(self):
        return ResourceConfig.list_env(self.name)

    def list_versions(self, start=0, limit=20):
        q = self.versions.order_by(Version.id.desc()).offset(start)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def list_containers(self, start=0, limit=20):
        from .container import Container
        q = self.containers.order_by(Container.id.desc()).offset(start)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def list_tasks(self, start=0, limit=20):
        from .task import Task
        q = self.tasks.order_by(Task.id.desc()).offset(start)
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def list_images(self, start=0, limit=20):
        from .image import Image
        return Image.list_by_app_id(self.id, start, limit)

    def add_version(self, sha):
        version = Version.create(sha, self.id)
        if not version:
            return None
        self.versions.append(version)
        db.session.add(self)
        db.session.commit()
        return version


event.listen(
    App.__table__,
    "after_create",
    DDL("ALTER TABLE %(table)s AUTO_INCREMENT = 10001;")
)

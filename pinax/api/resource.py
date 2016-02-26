from __future__ import unicode_literals

from functools import partial
from operator import attrgetter, itemgetter

from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.urlresolvers import reverse
from django.db.models.query import ModelIterable

from .exceptions import SerializationError


class ResourceIterable(ModelIterable):

    def __init__(self, resource_class, queryset):
        self.resource_class = resource_class
        super(ResourceIterable, self).__init__(queryset)

    def __iter__(self):
        for obj in super(ResourceIterable, self).__iter__():
            yield self.resource_class(obj)


class Resource(object):

    api_type = ""
    attributes = []
    relationships = {}
    bound_viewset = None

    @classmethod
    def from_queryset(cls, qs):
        return qs._clone(_iterable_class=partial(ResourceIterable, cls))

    @classmethod
    def populate(cls, data, obj=None):
        if obj is None:
            obj = cls.model()
        for k, v in data["attributes"].items():
            f = cls.model._meta.get_field(k)
            setattr(obj, f.attname, v)
        r = cls(obj)
        for k, v in data.get("relationships", {}).items():
            rel = cls.relationships[k]
            attr = rel.attr if rel.attr is not None else k
            if not rel.collection:
                f = cls.model._meta.get_field(attr)
                try:
                    o = f.rel.to._default_manager.get(pk=v["data"]["id"])
                except ObjectDoesNotExist:
                    raise ValidationError({
                        k: 'Relationship "{}" object ID {} does not exist'.format(
                            k, v["data"]["id"]
                        )
                    })
                setattr(obj, f.attname, o)
            else:
                # A collection can be:
                #  * ManyToManyField
                #  * Reverse relation
                given = set(map(itemgetter("id"), v["data"]))
                f = cls.model._meta.get_field(attr)
                if f in cls.model._meta.related_objects:
                    related = f.field.model
                    accessor_name = f.get_accessor_name()
                else:
                    related = f.rel.model
                    accessor_name = f.name
                qs = related._default_manager.filter(pk__in=given)
                found = set(map(attrgetter("id"), qs))
                missing = given.difference(found)
                if missing:
                    raise ValidationError({
                        k: 'Relationship "{}" object IDs {} do not exist'.format(
                            k,
                            ", ".join(sorted(missing))
                        )
                    })

                def save(self, parent=None):
                    if parent is None:
                        parent = obj
                    getattr(parent, accessor_name).add(*qs)
                obj.save_relationships = save
        return r

    def __init__(self, obj):
        self.obj = obj

    def create(self, **kwargs):
        self.obj.full_clean()
        self.obj.save()
        return self.obj

    def update(self, **kwargs):
        self.obj.full_clean()
        self.obj.save()
        return self.obj

    def save(self, create_kwargs=None, update_kwargs=None):
        if create_kwargs is None:
            create_kwargs = {}
        if update_kwargs is None:
            update_kwargs = {}
        if self.obj.pk is None:
            self.obj = self.create(**create_kwargs)
        else:
            self.obj = self.update(**update_kwargs)

    def get_identifier(self):
        return {
            "type": self.api_type,
            "id": str(self.id),
        }

    def get_self_link(self, request=None):
        kwargs = {}
        obj = None

        def resolve(r=None):
            nonlocal obj
            if r is None:
                r = self.__class__
            if r.bound_viewset is None:
                raise RuntimeError("cannot generate link without being bound to a viewset")
            viewset = r.bound_viewset
            if viewset.parent:
                resolve(viewset.parent)
            if obj is None:
                obj = self.obj
            else:
                obj = getattr(obj, viewset.url.lookup["field"])
            kwargs[viewset.url.lookup["field"]] = str(r(obj).id)
        resolve()
        url = reverse(
            "{}-detail".format(self.bound_viewset.url.base_name),
            kwargs=kwargs
        )
        if request is not None:
            return request.build_absolute_uri(url)
        return url

    def serializable(self, included=None, request=None):
        attributes = {}
        for attr in self.attributes:
            attributes[attr] = getattr(self.obj, attr)
        relationships = {}
        for name, rel in self.relationships.items():
            rel_obj = relationships.setdefault(name, {})
            if rel.collection:
                qs = getattr(self.obj, name).all()
                data = rel_obj.setdefault("data", [])
                for v in qs:
                    data.append(rel.resource_class(v).get_identifier())
            else:
                v = getattr(self.obj, name)
                if v is not None:
                    rel_obj["data"] = rel.resource_class(v).get_identifier()
                else:
                    rel_obj["data"] = None
        if included is not None:
            for path in included.paths:
                resolve_include(self, path, included)
        res = {
            "attributes": attributes,
        }
        if self.bound_viewset:
            res.update({"links": {"self": self.get_self_link(request=request)}})
        res.update(self.get_identifier())
        if relationships:
            res["relationships"] = relationships
        return res


def resolve_include(resource, path, included):
    try:
        head, rest = path.split(".", 1)
    except ValueError:
        head, rest = path, []
    if head not in resource.relationships:
        raise SerializationError("'{}' is not a valid relationship to include".format(head))
    rel = resource.relationships[head]
    for obj in getattr(resource.obj, head).all():
        resolve_include(resource, rest, included)
        included.add(rel.resource_class(obj))

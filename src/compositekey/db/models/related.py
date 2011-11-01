__author__ = 'aldaran'

from django.utils.translation import ugettext_lazy as _
from django.db.models.fields import Field
from django.db.models.fields.related import ForeignKey, ManyToOneRel, OneToOneField, OneToOneRel, SingleRelatedObjectDescriptor

from compositekey.utils import *
from compositekey.db.deletion import activate_delete_monkey_path
from compositekey.forms.models import activate_modelform_monkey_path


__all__ = ['MultipleFieldPrimaryKey', 'CompositeForeignKey', 'CompositeOneToOneField']


def _get_field(opts, name):
    if getattr(opts, "has_composite_primarykeys_field", False) and opts.composite_primarykeys_field.name == name:
        return (opts.composite_primarykeys_field, None, True, False)
    if getattr(opts, "has_composite_foreignkeys_field", False) and opts.composite_foreignkeys_fields.has_key(name):
        return (opts.composite_foreignkeys_fields.get(name), None, True, False)
    return None


def wrap_meta_prepare(opts, original_prepare):
    if hasattr(original_prepare, "_sign"):
        return original_prepare

    opts._lazy_prepare_field_actions = []
    def _prepare(model):
        for prepare_action in getattr(opts, "_lazy_prepare_field_actions", []): prepare_action()
        original_prepare(model)
    _prepare._sign = "composite"
    return _prepare

#def wrap_get_field(opts, original_get_field):
#    def get_field(name, many_to_many=True):
#        f = _get_field(opts, name)
#        return f[0] if f else original_get_field(name, many_to_many=many_to_many)
#    return get_field

def wrap_get_field_by_name(opts, original_get_field_by_name):
    def get_field_by_name(name):
        return _get_field(opts, name) or original_get_field_by_name(name)
    return get_field_by_name


def _get_composite_pk(fields, name="pk"):
    cache_name="_composite_%s_cache" % name
    def _get(obj):
        # cache, if change the values you can yet identify thre real record
        if not getattr(obj, cache_name, False):
            setattr(obj, cache_name, assemble_pk(*[getattr(obj, f.name) for f in fields]))
        return getattr(obj, cache_name)
    return _get

def _set_composite_pk(fields, name="pk"):
    cache_name="_composite_%s_cache" % name
    def _set(obj, value):
        values = disassemble_pk(value)
        if len(values) <> len(fields):
            values = [None for _ in fields]
        for field, val in zip(fields, values):
            setattr(obj, field.name, val)
        # reset pk cache only you are not deleting the model
        if len(disassemble_pk(value)) == len(fields):
            if hasattr(obj, cache_name):
                delattr(obj, cache_name)
                getattr(obj, name)
    return _set


def wrap_save_model(original_save):
    if hasattr(original_save, "_sign"):
        return original_save
    def save(obj, force_insert=False, force_update=False, using=None):
        ret = original_save(obj, force_insert=force_insert, force_update=force_update, using=using)
        # reset pk cache
        del obj._composite_pk_cache
        obj.pk
        return ret
    save.alters_data = True
    save._sign = "composite"
    return save

def wrap_init_model(original_init):
    if hasattr(original_init, "_sign"):
        return original_init
    def __init__(obj, *args, **kwargs):
        opts = obj._meta

        post_actions = []
        if getattr(opts, "has_composite_primarykeys_field", False) and kwargs.has_key(opts.composite_primarykeys_field.name):
            pk = kwargs.pop(opts.composite_primarykeys_field.name)
            post_actions.append(lambda : setattr(obj, opts.composite_primarykeys_field.name, pk))
            
        if getattr(opts, "has_composite_foreignkeys_field", False):
            for name in opts.composite_foreignkeys_fields.keys():
                if kwargs.has_key(name):
                    val = kwargs.pop(name)
                    post_actions.append(lambda : setattr(obj, name, val))

        original_init(obj, *args, **kwargs)

        for action in post_actions: action()
        # setup pk cache
        obj.pk
    __init__._sign = "composite"
    return __init__



class MultipleFieldPrimaryKey(Field):
    virtual = True
    description = _("Composite Primary Keys")

    empty_strings_allowed = False
    default_error_messages = {
        'invalid': _(u"'%s' value must be an integer."),
    }
    def __init__(self, *args, **kwargs):
        kwargs['primary_key'] = True
        self._field_names = kwargs.pop('fields', [])
        assert isinstance(self._field_names, list) and len(self._field_names) > 0, \
               "%ss must have fields=[..]." % self.__class__.__name__

        kwargs['blank'] = True
        super(MultipleFieldPrimaryKey, self).__init__(*args, **kwargs)

    def get_internal_type(self):
        return "MultipleFieldPrimaryKey"

    def to_python(self, value):
        return value

    def validate(self, value, model_instance):
        pass

    def get_prep_value(self, value):
        return None

    def contribute_to_class(self, cls, name):
        assert not getattr(cls._meta, "has_composite_primarykeys_field", False), \
               "A model can't have more than one MultipleFieldPrimaryKey."
        assert not cls._meta.has_auto_field, \
               "A model can't have one MultipleFieldPrimaryKey or AutoField."

        # impostiamo il default manager su quello composite
        from compositekey.db.manager import CompositeDefaultManager
        assert not hasattr(cls, "objects") or isinstance(cls.objects, CompositeDefaultManager), \
               "A model with an compotiteKey needs of CompositeDefaultManager."

        manager = CompositeDefaultManager()
        if not hasattr(cls, "objects"):
            cls.add_to_class('objects', manager)
        cls.add_to_class('_base_manager', manager)
        cls.add_to_class('_default_manager', manager)

        cls._meta._prepare = wrap_meta_prepare(cls._meta, cls._meta._prepare)
        super(MultipleFieldPrimaryKey, self).contribute_to_class(cls, name)
        cls._meta.has_composite_primarykeys_field = True
        cls._meta.composite_primarykeys_field = self
        cls._meta.composite_special_fields = getattr(cls._meta, "composite_special_fields", [])

        # needs to remove esplicit from fields (really is not a field)
        cls._meta.local_fields.remove(self)
        cls._meta.add_virtual_field(self)
        cls._meta.composite_special_fields.append(self)

        # todo: patch cls._meta.get_field for RELATED
        #cls._meta.get_field = wrap_get_field(cls._meta, cls._meta.get_field)
        #cls._meta.get_field_by_name = wrap_get_field_by_name(cls._meta, cls._meta.get_field_by_name)
        cls._meta.composite_get_field_by_name = wrap_get_field_by_name(cls._meta, cls._meta.get_field_by_name)

        cls.save = wrap_save_model(cls.save) # adding reset PK cache
        cls.__init__ = wrap_init_model(cls.__init__) # adding reset PK cache

        activate_delete_monkey_path()

        def lazy_init():
            fields = self.get_key_fields()
            names = [f.name for f in fields]
            cls._meta.ordering = cls._meta.ordering or names

            # TODO: better add primary key = () and not unique
            # example: PRIMARY KEY (album, disk, posn)

            if names not in cls._meta.unique_together:
                cls._meta.unique_together.append(names)
            for field in fields: field.db_index=True

            # get/set PK propery
            setattr(cls, cls._meta.pk.attname, property(_get_composite_pk(fields), _set_composite_pk(fields)))

        cls._meta._lazy_prepare_field_actions.append(lazy_init)

    def formfield(self, **kwargs):
        return None

    def get_key_fields(self):
        fields = []
        for f in [self.model._meta.composite_get_field_by_name(name)[0] for name in self._field_names]:
            if isinstance(f, _CompositeForeignKey):
                fields += [nf for nf in f.fields]
            else:
                fields.append(f)
        return fields

class CompositeManyToOneRel(ManyToOneRel):
    def get_related_field(self):
        opts = self.to._meta
        if getattr(opts, "has_composite_primarykeys_field", False) and opts.composite_primarykeys_field.name == self.field_name:
            return opts.composite_primarykeys_field
        return super(CompositeManyToOneRel, self).get_related_field()

class CompositeOneToOneRel(OneToOneRel):
    def get_related_field(self):
        opts = self.to._meta
        if getattr(opts, "has_composite_primarykeys_field", False) and opts.composite_primarykeys_field.name == self.field_name:
            return opts.composite_primarykeys_field
        return super(CompositeManyToOneRel, self).get_related_field()


def prepare_hidden_key_field(model, field, ext={}, prefix="composite"):
    default = ext.get(field.name, {})
    import copy
    new_field = copy.deepcopy(field)
    # bk
    new_field.fk_fieldname = new_field.name
    new_field.model = model
    new_field.db_column = default.get("db_column", "%s_%s" % (prefix, new_field.db_column or new_field.name))
    new_field.name = default.get("name", "%s_%s" % (prefix, new_field.name))
    new_field.attname = default.get("attname", "%s_%s" % (prefix, new_field.attname))
    new_field.verbose_name = default.get("verbose_name", "%s %s" % (prefix, new_field.verbose_name))
    new_field.column = default.get("column", "%s_%s" % (prefix, new_field.column))
    
    # hide formfield (None)
    new_field.formfield = lambda *args, **kwargs : None
    #new_field.formfield = lambda *args, **kwargs : forms.CharField(required=False, widget=forms.TextInput(attrs={"readonly" : True}))
    return new_field

def wrap_setter(original_set, name, fields):
    cache_name="_composite_%s_cache" % name
    def __set__(obj, value):
        original_set(obj, value)
        for field in fields:
            setattr(obj, field.name, getattr(value, field.fk_fieldname, field.default))
        delattr(obj, cache_name)
        getattr(obj, name)
    return __set__




class _CompositeForeignKey(object):
    virtual = True

    def contribute_to_class(self, cls, name):
        opts = cls._meta
        # impostiamo il default manager su quello composite
        from compositekey.db.manager import CompositeDefaultManager
        assert not hasattr(cls, "objects") or isinstance(cls.objects, CompositeDefaultManager), \
               "A model with an compotiteKey needs of CompositeDefaultManager."

        opts._prepare = wrap_meta_prepare(opts, opts._prepare)
        super(_CompositeForeignKey, self).contribute_to_class(cls, name)

        opts.has_composite_foreignkeys_field = True
        opts.composite_foreignkeys_fields = getattr(opts, "composite_foreignkeys_fields", {})
        opts.composite_foreignkeys_fields[name]=self
        opts.composite_special_fields = getattr(opts, "composite_special_fields", [])

        if not opts.abstract:
            manager = CompositeDefaultManager()
            if not hasattr(cls, "objects"):
                cls.add_to_class('objects', manager)
            cls.base_manager = manager
            #cls.add_to_class('_default_manager', manager)

        # needs to remove esplicit from fields (really is not a field)
        opts.local_fields.remove(self)
        opts.add_virtual_field(self)
        opts.composite_special_fields.append(self)

        activate_modelform_monkey_path()
        related_field = self.rel.get_related_field()

        cls.__init__ = wrap_init_model(cls.__init__) # adding reset PK cache

        # add the real composition fields
        if not getattr(self, "_initialized_new_fields", False):
            print 88888888, cls, self

            new_fields = [prepare_hidden_key_field(cls, f, self.fields_ext, prefix=name) for f in related_field.get_key_fields()]
            for f in new_fields: cls.add_to_class(f.name, f)
            self.fields = new_fields

            # hack to say to DB query to retrieve a real column
            #self.column = new_fields[1].column

            # get/set _id propery
            setattr(cls, "%s_id" % name, property(_get_composite_pk(new_fields, name=name), _set_composite_pk(new_fields, name=name)))

            # hack add wrap setter related
            reverse_desc = getattr(cls, name)
            reverse_desc.__set__ = wrap_setter(reverse_desc.__set__, name, new_fields)

            if self.unique:
                names = [f.name for f in new_fields]
                if names not in opts.unique_together:
                    opts.unique_together.append([f.name for f in new_fields])
        self._initialized_new_fields = True





    def contribute_to_related_class(self, cls, related):
        super(_CompositeForeignKey, self).contribute_to_related_class(cls, related)

    def save_form_data(self, instance, data):
        print "save", instance, data
        super(_CompositeForeignKey, self).save_form_data(instance, data)

class CompositeForeignKey(_CompositeForeignKey, ForeignKey):
    def __init__(self, to, to_field=None, rel_class=CompositeManyToOneRel, fields_ext={}, **kwargs):
        super(_CompositeForeignKey, self).__init__(to, to_field=to_field, rel_class=rel_class, **kwargs)
        self.fields_ext = fields_ext

class CompositeOneToOneField(_CompositeForeignKey, OneToOneField):
    def __init__(self, to, to_field=None, rel_class=CompositeOneToOneRel, fields_ext={}, **kwargs):
        kwargs['unique'] = True
        # hack to avoid super in OneToOneField (break estendubility)
        ForeignKey.__init__(self, to, to_field=to_field, rel_class=rel_class, **kwargs)
        self.fields_ext = fields_ext


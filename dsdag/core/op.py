import abc
from toposort import toposort
import copy
import uuid
import types
import interactive_data_tree as idt
import time
import logging
import pandas as pd

from parameter import BaseParameter, UnhashableParameter

class OpMeta(type):
    """Updates doc strings on creation of new op by scanning op parameters"""
    _i = 0
    def __new__(cls, name, parents, dct):
        # Both brand new Ops and derived ones will enter this method
        _cls =  super(OpMeta, cls).__new__(cls, name, parents, dct)
        docs = name
        docs += '\n\n'
        docs += 'Parameters\n'
        docs += '----------\n'
        docs += "\n".join("%s : %s" % (k, str(param.help_msg))
                          for k, param in
                          _cls.scan_for_op_parameters(overrides=dict()).items())
        docs += '\n\n\n'

        _cls.__doc__ = docs

        return _cls

    def __call__(cls, *args, **kwargs):
        o = type.__call__(cls, *args, **kwargs)
        return o

class OpVertex(object):
    __metaclass__ = OpMeta
    _never_cache = False
    _instance_id_map = dict()
    _given_name_cnt_map = dict()
    _closure_map = dict()
    def __init__(self, **kwargs):
        # For parameters:
        #   (1) An attribute stores the full (Base)Parameter Instance
        #       - store under parameters
        #   (2) The value of each parameter is assigned to self

        # Filled out during build call
        self._built = False
        self.req_hash = None

        self._dag = None
        self._user_kwargs = kwargs
        self._parameters = self.scan_for_op_parameters(overrides=self._user_kwargs)
        self._name = kwargs.get('name', None)
        if self._name is not None:
            name_iid = self.__class__._given_name_cnt_map.get(self._name, 0)
            new_name = self._name
            if name_iid > 0:
                new_name = self._name + '_' + str(name_iid)
            self.__class__._given_name_cnt_map[self._name] = name_iid + 1
            self._name = new_name

        self.unique_cls_name = str(self.__class__.__name__)
        iid = self.__class__._instance_id_map.get(self.unique_cls_name)
        if iid is not None:
            self.__class__._instance_id_map[self.unique_cls_name] += 1
            self.unique_cls_name += '_%s' % str(iid)
        else:
            self.__class__._instance_id_map[self.unique_cls_name] = 1

        for p_n, p, in self._parameters.items():
            setattr(self, p_n, p.value)

        self._cacheable = not self._never_cache

    def __update_doc_str(self):
        #docs = self.__class__.__name__
        docs = self.unique_cls_name
        docs += '\n'

        docs += "\n".join("%s : %s" % (k, str(param.help_msg))
                          for k, param in self._parameters.items())
        self.__doc__ = docs

    def __call__(self, *args, **kwargs):
        return self.with_requires(*args, **kwargs)

    def __repr__(self):
        params = self.get_parameters()
        repr = ", ".join("%s=%s" % (str(k), str(params[k]))
                        for k in sorted(params.keys()))
        if self._name is not None:
            repr += ', name=\'' + self._name + '\''
        return self.__class__.__name__ + "(" + repr + ")"
        #return self._name + "(" + repr + ")"

    def __hash__(self):
        p_tuples = tuple([(k, repr(self._parameters[k]))
                          for k in sorted(self._parameters.keys())])
        #r_tuples = tuple([(k, repr(self._requirements[k]))
        #                  for k in sorted(self._requirements.keys())])
        if self.req_hash is not None:
            r = self.req_hash
        else:
            r = self.requires.__func__

        return hash((type(self), p_tuples, r))
        #return hash((self.unique_cls_name, p_tuples, r_tuples))

    def req_match(self, other):
        return self.requires.__func__ == other.requires.__func__

    def old_req_match(self, other):
        self_reqs = self._dag.dep_map[self]
        other_reqs = self._dag.dep_map[other]

        if len(self_reqs) != len(other_reqs):
            return False

        for req_name, req_o in self_reqs.items():
            if req_name not in other_reqs:
                return False

            if req_o != other_reqs[req_name]:
                return False

        return True

    def param_match(self, other):
        self_params = self.get_parameters()
        other_params = other.get_parameters()

        if len(self_params) != len(other_params):
            return False

        for p_k, p_v in self_params.items():
            if p_k not in other_params:
                return False

            if not(p_v == other_params[p_k]):
                return False

        return True

    def __hash__eq__(self, other):
        return hash(self) == hash(other)

    def __eq__(self, other):
        if not isinstance(other, OpVertex) and not issubclass(type(other), OpVertex):
            return False

        if not self.param_match(other):
            return False

        if not self.req_match(other):
            return False

        if not isinstance(self, type(other)):# and not issubclass(type(self), type(other)):
            return False

        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def _set_dag(self, dag):
        from dsdag.core.dag import DAG
        if not isinstance(dag, DAG) or not issubclass(type(dag), DAG):
            msg = "Expected a DAG object or derived, got %s" % str(type(dag))
            raise ValueError(msg)
        self._dag = dag

    def get_logger(self, log_level='WARN'):
        if self._dag is not None:
            l = self._dag.logger
        else:
            l = logging.getLogger()
            l.setLevel(log_level)
        return l

    def get_name(self):
        return self._name if self._name is not None else self.unique_cls_name

    def _node_color(self):
        return 'lightblue2'

    def _node_style(self):
        return 'filled'

    def _node_shape(self):
        return 'oval'

    def _get_viz_attrs(self):
        return dict(color=self._node_color(),
                    style=self._node_style(),
                    shape=self._node_shape())

    def set_cacheable(self, is_cacheable):
        self._cacheable = is_cacheable

    def get_input_ops(self):
        return self._dag.get_op_input(self)

    @classmethod
    def passthrough_params(cls, dest_cls, cls_name, on_conflict='error', skip_params=None,
                           requirement_name=None):
        """



        :param dest_cls:
        :param cls_name:
        :param on_conflict:
        :param skip_params:
        :param requirement_name:
        :return:
        """
        params = cls.scan_for_op_parameters(overrides=dict())
        attrs_params = dict()
        for p_name, p in params.items():
            if skip_params is not None and p_name in skip_params:
                continue

            if hasattr(dest_cls, p_name):
                if on_conflict == 'use_src':
                    #setattr(new_cls, p_name, p)
                    attrs_params[p_name] = p
                elif on_conflict == 'use_dest':
                    pass
                else:
                    raise ValueError("Param with name %s already exists on %s" % (p_name, str(dest_cls)))
            else:
                #setattr(new_cls, p_name, p)
                attrs_params[p_name] = p

        if requirement_name is not None:
            def req_closure(self):
                return {requirement_name: cls(**{
                    _pname: getattr(self, _pname)
                    for _pname, _p in attrs_params.items()
                })}
            attrs_params['requires'] = req_closure

        new_cls = OpMeta(cls_name, (dest_cls,),
                         attrs_params)

        return new_cls

    @classmethod
    def passthrough_requirements(cls, skip_params=None, on_conflict='error', **pass_reqs):
        """"
        A Pass through requirement inherits it's parent's parameters and constructs a requires method that
        instatiate those requirements and passes the param values through (i.e. passthrough)
        """
        # Mix inputs (mixin)?
        #params = cls.scan_for_op_parameters(overrides=dict())
        #attrs_params = dict()
        dst_cls = cls
        for r_name, r_op in pass_reqs.items():
            dst_cls = r_op.passthrough_params(dst_cls, "Passthrough" + cls.__name__,
                                              on_conflict=on_conflict, skip_params=skip_params)


    @classmethod
    def scan_for_op_parameters(cls, overrides=None):
        """ Scans the class for BaseParameter types and returns mapping to them"""
        params = dict()
        for o_n in dir(cls):
            o = getattr(cls, o_n)
            if isinstance(o, BaseParameter) or issubclass(type(o), BaseParameter):
                if o_n in BaseParameter._reserved_kw:
                    raise ValueError("Parameter cannot use reserved keyword '%s'" % o_n)
                params[o_n] = copy.copy(o)

                if overrides is not None and o_n in overrides:
                    params[o_n].set_value(overrides[o_n])
        return params

    @classmethod
    def copy_op_parameters(cls, other, default_overrides=None):
        params = cls.scan_for_op_parameters(overrides=default_overrides)
        for o_n, o in params.items():
            setattr(other, o_n, o)

    def with_requires_old(self, *args, **kwargs):
        if len(kwargs) == 0 and len(args) == 0:
            return self
        elif len(kwargs) != 0:
            req_ret = kwargs
        elif len(args) != 0:
            from dsdag.ext.misc import VarOp
            # not supports *args just yet
            req_ret = list(args)
            req_ret = [a if isinstance(a, OpVertex) or issubclass(a.__class__, OpVertex)
                       else VarOp(obj=a)
                       for a in req_ret]

        else:
            msg = "Mix of *args and **kwargs not supported (yet?)"
            raise ValueError(msg)

        def closure(self):
            return req_ret
        # Define new Op class that has correct requires
        new_class = OpMeta(self.unique_cls_name, (type(self),),
                           {'requires':closure,
                            '_op_lineage_id': self._instance_id})

        return new_class(**self._user_kwargs)

    def with_requires(self, *args, **kwargs):
        if len(kwargs) == 0 and len(args) == 0:
            return self
        elif len(kwargs) != 0:
            from dsdag.ext.misc import VarOp
            req_ret = kwargs
            req_hash = hash(tuple((k, v if isinstance(v, OpVertex) or issubclass(v.__class__, OpVertex) else VarOp(obj=v))
                        for k, v in kwargs.items()))
            #req_hash = hash(tuple(req_ret))
        elif len(args) != 0:
            from dsdag.ext.misc import VarOp
            # not supports *args just yet
            req_ret = list(args)
            req_ret = [a if isinstance(a, OpVertex) or issubclass(a.__class__, OpVertex)
                       else VarOp(obj=a)
                       for a in req_ret]
            req_hash = hash(tuple(req_ret))

        else:
            msg = "Mix of *args and **kwargs not supported (yet?)"
            raise ValueError(msg)


        if req_hash not in self.__class__._closure_map:
            def closure(self):
                return req_ret
            self.__class__._closure_map[req_hash] = closure

        # Use descriptor protocol: https://docs.python.org/2/howto/descriptor.html
        #self.requires = closure.__get__(self)
        self.req_hash = req_hash
        self.requires = self.__class__._closure_map[self.req_hash].__get__(self)

        return self

    @classmethod
    def with_params(cls, op_name=None, **kwargs):
        for k, v in kwargs.items():
            assert (isinstance(v, BaseParameter)
                    or issubclass(type(v), BaseParameter))
        name = (cls.__name__ + "WithParams") if op_name is None else op_name
        new_c = type(name,
                     (cls,),
                     kwargs)
        return new_c

    def get_parameters(self):
        return self._parameters

    def requires(self):
        return dict()

    def run(self):
        raise NotImplementedError("Implement run")

    def build(self, **dag_kwargs):
        from dsdag.core.dag import DAG
        return DAG(self, **dag_kwargs)

    def op_nb_viz(self, op_out, viz_out=None):
        raise NotImplementedError()
        from IPython.display import display
        import ipywidgets as widgets
        if viz_out is None:
            viz_out = widgets.Output()

        viz_out.append_display_data("op_nb_viz not implemented!")
        return viz_out





##############
if __name__ == """__main__""":
    pass

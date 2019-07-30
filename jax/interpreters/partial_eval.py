# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import itertools as it
from collections import namedtuple, Counter, defaultdict

import numpy as onp

from .. import core
from .. import linear_util as lu
from ..abstract_arrays import ShapedArray, ConcreteArray
from ..linear_util import thunk, transformation, transformation_with_aux
from ..util import unzip2, safe_zip, safe_map, toposort, partial
from ..core import (Trace, Tracer, new_master, Jaxpr, JaxprEqn, Literal,
                    get_aval, AbstractValue, unit, unitvar,
                    Primitive, call_p, TypedJaxpr, new_jaxpr_eqn)

map = safe_map
zip = safe_zip
def identity(x): return x

# A partial value (pval) is modeled as a pair (pv, const), as per
#   type PVal = (PV, Const)
#   data PV = NonePV | AbstractPV AbstractValue
#   type Const = MaybeTraced JaxType
# where the NonePV arm indicates a known (constant) value, the AbstractPV arm
# indicates an unknown value.
# Additionally, when the pv is an AbstractValue, then the const must be unit.


class JaxprTrace(Trace):
  def pure(self, val):
    if type(val) in core.literalable_types and onp.shape(val) == ():
      return JaxprTracer(self, PartialVal((None, val)), Literal(val))
    else:
      return self.new_const(val)

  def lift(self, val):
    return self.new_const(val)

  def sublift(self, val):
    return JaxprTracer(self, val.pval, FreeVar(val))

  def new_const(self, val):
    if isinstance(val, Tracer) and val.trace.level == self.level:
      raise Exception
    return JaxprTracer(self, PartialVal((None, val)), unit)

  def new_instantiated_literal(self, val):
    return JaxprTracer(self, PartialVal((get_aval(val), unit)), Literal(val))

  def new_instantiated_const(self, val):
    return JaxprTracer(self, PartialVal((get_aval(val), unit)), ConstVar(val))

  def new_arg(self, pval):
    _, const = pval
    return JaxprTracer(self, pval, LambdaBinding())

  def instantiate_const(self, tracer):
    pv, const = tracer.pval
    if isinstance(pv, AbstractValue):
      return tracer
    elif pv is None:
      if type(tracer.recipe) is Literal:
        return self.new_instantiated_literal(tracer.recipe.val)
      else:
        return self.new_instantiated_const(const)
    else:
      raise TypeError(pv)

  def process_primitive(self, primitive, tracers, params):
    assert not primitive.multiple_results, "update it"
    if primitive in custom_partial_eval_rules:
      assert False, "update to match new partial_eval"
      partial_eval = custom_partial_eval_rules[primitive]
      return partial_eval(self, *tracers, **params)
    else:
      pvs, consts = unzip2(t.pval for t in tracers)
      if all(pv is None for pv in pvs):
        return primitive.bind(*consts, **params)
      tracers = map(self.instantiate_const, tracers)
      avals = [t.aval for t in tracers]
      out_aval = primitive.abstract_eval(*avals, **params)
      out_tracer = JaxprTracer(self, PartialVal((out_aval, unit)), None)
      # TODO(dougalm): think about whether these ref cycles will leak memory
      out_tracer.recipe = new_jaxpr_eqn(tracers, [out_tracer], primitive, (), params)
      return out_tracer

  def process_call(self, call_primitive, f, tracers, params):
    if call_primitive in map_primitives:
      return self.process_map(call_primitive, f, tracers, params)
    in_pvs, in_consts = unzip2([t.pval for t in tracers])
    fun, aux = partial_eval(f, self, in_pvs)
    stuff = call_primitive.bind(fun, *in_consts, **params)
    out_pvs, jaxpr, env = aux()
    N = len(stuff) - len(jaxpr.constvars)
    out_pv_consts, consts = (stuff[:N], stuff[N:])
    const_tracers = map(self.new_instantiated_const, consts)
    bound_subjaxpr = (jaxpr, const_tracers, map(self.full_raise, env))
    out_tracers = [JaxprTracer(self, PartialVal((out_pv, out_pv_const)), None)
                   for out_pv, out_pv_const in zip(out_pvs, out_pv_consts)]
    eqn = new_jaxpr_eqn(tracers, out_tracers, call_primitive, (bound_subjaxpr,), params)
    for t in out_tracers:
      t.recipe = eqn
    return out_tracers

  def process_map(self, map_primitive, f, tracers, params):
    in_pvs, in_consts = unzip2([t.pval for t in tracers])
    reduced_pvs = map(remove_axis_from_pv, in_pvs)
    fun, aux = partial_eval(f, self, reduced_pvs)
    out_const, consts = map_primitive.bind(fun, *in_consts, **params)
    out_pv_reduced, jaxpr, env = aux()
    out_pv = add_axis_to_pv(params['axis_size'], out_pv_reduced)
    const_tracers = map(self.new_instantiated_const, consts)
    jaxpr_converted = jaxpr.copy()
    jaxpr_converted.constvars = []
    jaxpr_converted.invars = list(it.chain(jaxpr.constvars, jaxpr.invars))
    invars = tuple(it.chain(const_tracers, tracers))
    bound_subjaxpr = (jaxpr_converted, (), map(self.full_raise, env))
    eqn = JaxprEqn(invars, None, map_primitive, (bound_subjaxpr,), params)
    return JaxprTracer(self, PartialVal((out_pv, out_const)), eqn)

  def post_process_call(self, call_primitive, out_tracers, params):
    jaxpr, consts, env = tracers_to_jaxpr([], out_tracers)
    out_pvs, out_pv_consts = unzip2(t.pval for t in out_tracers)
    out = out_pv_consts + consts
    del consts, out_pv_consts
    master = self.master
    def todo(x):
      n = len(jaxpr.outvars)
      out_pv_consts, consts = x[:n], x[n:]
      trace = JaxprTrace(master, core.cur_sublevel())
      const_tracers = map(trace.new_instantiated_const, consts)
      env_tracers = map(trace.full_raise, env)
      bound_subjaxpr = (jaxpr, const_tracers, env_tracers)
      out_tracers = [JaxprTracer(trace, PartialVal((out_pv, out_pv_const)), None)
                     for out_pv, out_pv_const in zip(out_pvs, out_pv_consts)]
      eqn = new_jaxpr_eqn([], out_tracers, call_primitive, (bound_subjaxpr,), params)
      for t in out_tracers:
        t.recipe = eqn
      return out_tracers
    return out, todo

map_primitives = set()


def remove_axis_from_pv(pv):
  if pv is None:
    return pv
  elif isinstance(pv, AbstractValue):
    return remove_axis_from_aval(pv)
  else:
    raise TypeError(type(pv))

def remove_axis_from_aval(aval):
  if isinstance(aval, ShapedArray):
    # might be raising abstraction level from Concrete here
    return ShapedArray(aval.shape[1:], aval.dtype)
  else:
    raise NotImplementedError  # TODO(mattjj)

def add_axis_to_pv(size, pv):
  if pv is None:
    return pv
  elif isinstance(pv, AbstractValue):
    return add_axis_to_aval(size, pv)
  else:
    raise TypeError(type(pv))

def add_axis_to_aval(size, aval):
  if isinstance(aval, ShapedArray):
    return ShapedArray((size,) + aval.shape, aval.dtype)
  else:
    raise NotImplementedError  # TODO(mattjj)


def partial_eval(f, trace, pvs):
  f = trace_to_subjaxpr(f, trace.master, False)
  return partial_eval_wrapper(f, tuple(pvs))


@transformation_with_aux
def partial_eval_wrapper(avals, *consts):
  py_args = (map(PartialVal, zip(avals, consts)),)
  jaxpr, (out_pvals, consts, env) = yield py_args, {}
  out_pvs, out_consts = unzip2(out_pvals)
  out = tuple(out_consts) + tuple(consts)  # TODO: can consts be traced?
  yield out, (out_pvs, jaxpr, env)


def abstract_eval_fun(fun, *avals, **params):
  pvals_in = [PartialVal((a, unit)) for a in avals]
  _, pval_outs, _ = trace_to_jaxpr(lu.wrap_init(fun, params), pvals_in,
                                  instantiate=True)
  avals_out, _ = unzip2(pval_out)
  for aval_out in avals_out:
    assert isinstance(aval_out, AbstractValue)  # instantiate=True
  assert False, "Need callers to handle multiple avals_out now"
  return avals_out


class JaxprTracer(Tracer):
  __slots__ = ['pval', 'recipe']

  def __init__(self, trace, pval, recipe):
    assert isinstance(pval, PartialVal)
    pv, const = pval
    if isinstance(const, Tracer):
      assert const.trace.level < trace.level
    self.trace = trace
    self.pval = pval
    self.recipe = recipe

  def __repr__(self):
    return 'Traced<{}:{}>'.format(self.aval, self.trace)

  @property
  def aval(self):
    pv, const = self.pval
    return partial_val_aval(pv, const)

  @property
  def parents(self):
    if isinstance(self.recipe, JaxprEqn):
      return eqn_parents(self.recipe)
    elif isinstance(self.recipe, Destructuring):
      return eqn_parents(self.recipe.eqn)
    else:
      return []

  def ispure(self):
    pv, _ = self.pval
    return pv is None  # or pv is core.abstract_unit

  def full_lower(self):
    if self.ispure():
      _, const = self.pval
      return core.full_lower(const)
    else:
      return self

Destructuring = namedtuple('Destructuring', ['i', 'eqn', 'key'])

class PartialVal(tuple):
  def __new__(cls, xs):
    pv, const = xs
    if not core.skip_checks:
      # type checks
      assert isinstance(pv, valid_pv_types), xs
      assert isinstance(const, core.Tracer) or core.valid_jaxtype(const), xs
      # invariant checks
      if isinstance(pv, AbstractValue):
        assert const == core.unit, xs
    return tuple.__new__(cls, xs)

valid_pv_types = (AbstractValue, type(None))

def merge_pvals(val, pval):
  pv, const = pval
  if isinstance(pv, AbstractValue):
    return val
  elif pv is None:
    return const
  else:
    raise TypeError(pv)

def join_pvals(pval1, pval2):
  assert False, "update it"
  pv1, const1 = pval1
  pv2, const2 = pval2
  if pv1 is None and pv2 is None:
    aval1, aval2 = core.get_aval(const1), core.get_aval(const2)
    if aval1 == aval2:
      return pval1  # both pvals known, equal constants
    else:
      aval = core.lattice_join(aval1, aval2)
      return PartialVal((aval, unit))  # both pvals known, different constants
  elif pv1 is None and isinstance(pv2, AbstractValue):
    aval = pv2
    return PartialVal((aval, unit))  # first pval known, second not known
  elif isinstance(pv1, AbstractValue) and pv2 is None:
    aval = pv1
    return PartialVal((aval, unit))  # first pval not known, second known
  elif isinstance(pv1, AbstractValue) and isinstance(pv2, AbstractValue):
    aval = core.lattice_join(pv1, pv2)
    return PartialVal((aval, unit))  # neither is known
  else:
    # the pvals are tuples with some mixtures of known/unknown
    assert isinstance(pv1, JaxprTracerTuple) or isinstance(pv2, JaxprTracerTuple)
    def explode(pv, const):
      if isinstance(pv, AbstractValue):
        assert const == core.unit
        const = [core.unit] * len(pv)
      elif pv is None:
        pv = [None] * len(const)
      else:
        assert isinstance(pv, JaxprTracerTuple)
      return pv, const
    pv1, const1 = explode(pv1, const1)
    pv2, const2 = explode(pv2, const2)
    pvals1, pvals2 = zip(pv1, const1), zip(pv2, const2)
    join_pvs, join_consts = unzip2(map(join_pvals, pvals1, pvals2))
    if all(isinstance(pv, AbstractValue) for pv in join_pvs):
      assert all(const == core.unit for const in join_consts)
      return PartialVal((AbstractTuple(join_pvs), core.unit))
    else:
      return PartialVal((JaxprTracerTuple(join_pvs), pack(join_consts)))

def as_abstract_val(pv):
  if isinstance(pv, AbstractValue):
    return pv
  elif isinstance(pv, JaxprTracerTuple):
    return AbstractTuple(map(as_abstract_val, pv))
  elif pv is None:
    raise TypeError("{} is not abstract".format(pv))

def partial_val_aval(pv, const):
  if isinstance(pv, AbstractValue):
    return pv
  elif pv is None:
    return get_aval(const)
  else:
    raise TypeError(pv)


def abstractify(x):
  return PartialVal((core.concrete_aval(x), unit))

def trace_unwrapped_to_jaxpr(fun, pvals, instantiate, **kwargs):
  return trace_to_jaxpr(lu.wrap_init(fun, kwargs), pvals,
                        instantiate=instantiate)

def trace_to_jaxpr(fun, pvals, **kwargs):
  """Traces a function, given abstract inputs, to a jaxpr."""
  instantiate = kwargs.pop('instantiate', False)
  with new_master(JaxprTrace) as master:
    fun = trace_to_subjaxpr(fun, master, instantiate)
    jaxpr, (out_pvals, consts, env) = fun.call_wrapped(pvals)
    assert not env
    del master

  return jaxpr, out_pvals, consts

@transformation
def trace_to_subjaxpr(master, instantiate, pvals):
  assert all([isinstance(pv, PartialVal) for pv in pvals]), pvals
  trace = JaxprTrace(master, core.cur_sublevel())
  in_tracers = map(trace.new_arg, pvals)
  ans = yield in_tracers, {}
  out_tracers = map(trace.full_raise, map(core.full_lower, ans))
  out_tracers = map(partial(instantiate_const_at, trace, instantiate), out_tracers)
  jaxpr, consts, env = tracers_to_jaxpr(in_tracers, out_tracers)
  out_pvals = [t.pval for t in out_tracers]
  del trace, in_tracers, out_tracers
  yield jaxpr, (out_pvals, consts, env)

def instantiate_const_at(trace, instantiate, tracer):
  assert type(instantiate) is bool
  if instantiate:
    return trace.instantiate_const(trace.full_raise(tracer))
  else:
    return tracer


FreeVar = namedtuple('FreeVar', ['val'])
ConstVar = namedtuple('ConstVar', ['val'])
LambdaBinding = namedtuple('LambdaBinding', [])

def eqn_tracer_to_var(var, eqn):
  _, in_tracers, out_tracers, primitive, bound_subjaxprs, params = eqn
  invars  = map(var, in_tracers)
  outvars = map(var, out_tracers)
  new_bound_subjaxprs = [(j, map(var, c), map(var, f))
                         for j, c, f in bound_subjaxprs]
  return new_jaxpr_eqn(invars, outvars, primitive, new_bound_subjaxprs, params)


def tracers_to_jaxpr(in_tracers, out_tracers):
  newvar = gensym('')
  t_to_var = defaultdict(newvar)
  var = lambda t: t_to_var[id(t)]
  sorted_tracers = toposort(out_tracers)
  invars = map(var, in_tracers)
  eqns = []
  env = {}
  consts = {}
  const_to_var = defaultdict(newvar)
  destructuring_vars = {}
  processed_eqns = set()
  for t in sorted_tracers:
    recipe = t.recipe
    if isinstance(recipe, JaxprEqn):
      if recipe.eqn_id not in processed_eqns:
        eqns.append(eqn_tracer_to_var(var, recipe))
        processed_eqns.add(recipe.eqn_id)
    elif isinstance(recipe, LambdaBinding):
      assert any(t is in_tracer for in_tracer in in_tracers)
      assert in_tracers, "Lambda binding with no args"
    elif isinstance(recipe, FreeVar):
      env[var(t)] = recipe.val
    elif isinstance(recipe, ConstVar):
      v = t_to_var[id(t)] = const_to_var[id(recipe.val)]
      consts[v] = recipe.val
    elif isinstance(recipe, Literal):
      t_to_var[id(t)] = recipe
    elif recipe is unit:
      t_to_var[id(t)] = unitvar
    else:
      raise TypeError(recipe)

  env_vars, env_vals = unzip2(env.items())
  const_vars, const_vals = unzip2(consts.items())
  jaxpr = Jaxpr(const_vars, env_vars, invars, list(map(var, out_tracers)), eqns)
  # core.skip_checks or core.check_jaxpr(jaxpr)
  core.check_jaxpr(jaxpr)
  return jaxpr, const_vals, env_vals


def gensym(suffix):
  counter = it.count()
  return lambda: Var(next(counter), suffix)

class Var(object):
  def __init__(self, count, suffix):
    self.count = count
    self.suffix = suffix

  def __repr__(self):
    rem = self.count
    s = ''
    while True:
      rem, i = rem // 26, rem % 26
      s = chr(97 + i % 26) + s
      if not rem:
        break
    return s + self.suffix

def eqn_parents(eqn):
  subjaxpr_tracers = [it.chain(c, f) for _, c, f in eqn.bound_subjaxprs]
  return list(it.chain(eqn.invars,  *subjaxpr_tracers))

def as_pval(aval, is_unknown, val):
  assert type(is_unknown) is bool
  if is_unknown:
    return PartialVal((aval, core.unit))
  else:
    return PartialVal((None, val))

def as_pval2(aval, is_unknown):
  assert type(is_unknown) is bool
  if is_unknown:
    return PartialVal((core.abstract_unit, core.unit))
  else:
    return PartialVal((aval, core.unit))

def unknown(x):
  if x is None:
    return False
  elif isinstance(x, core.AbstractValue):
    return True
  else:
    raise TypeError(type(x))

def _closure_convert_jaxpr(jaxpr):
  core.skip_checks or core.check_jaxpr(jaxpr)
  lifted_jaxpr = jaxpr.copy()
  lifted_jaxpr.constvars = ()
  lifted_jaxpr.invars = [tuple(jaxpr.constvars)] + list(jaxpr.invars)
  core.skip_checks or core.check_jaxpr(lifted_jaxpr)
  return lifted_jaxpr

def partial_eval_jaxpr(jaxpr, second_components, instantiate):
  assert False, "update it"
  # jaxpr :: d -> c -> a -> (c, b)
  f = lu.wrap_init(core.jaxpr_as_fun(jaxpr))

  cell = []
  # we do some final-style output munging to place residuals
  # fun :: d1 -> c1 -> a1 -> (c1, (b1, res))
  def fun(*vals):
    pvals = map(as_pval, jaxpr.in_avals, second_components, vals)
    jaxpr_2, out_pval, consts_2 = trace_to_jaxpr(f, pvals, instantiate=instantiate)
    out_pv, out_const = out_pval
    out_pv = (None, None) if out_pv is None else out_pv
    out_const = (core.unit, core.unit) if out_const is core.unit else out_const
    out_pv_c, out_pv_b = out_pv
    out_const_c, out_const_b = out_const
    cell.append((out_pv_c, out_pv_b, jaxpr_2))
    return pack((out_const_c, pack((out_const_b, pack(consts_2)))))

  pvals = map(as_pval2, jaxpr.in_avals, second_components)
  jaxpr_1, out_pval, consts_1 = trace_to_jaxpr(
      lu.wrap_init(fun), pvals, instantiate=True)
  out_pv_c, out_pv_b, jaxpr_2 = cell[0]

  #               jaxpr_1 :: d1 -> c1 -> a1 -> (c1, (b1, res))
  #               jaxpr_2 :: res | d2 -> c2 -> a2 -> (c2, b2)
  #        lifted_jaxpr_2 :: res -> d2 -> c2 -> a2 -> (c2, b2)
  # doubly_lifted_jaxpr_2 :: d2 -> c2 -> (a2, res) -> (c2, b2)
  lifted_jaxpr_2 = _closure_convert_jaxpr(jaxpr_2)
  doubly_lifted_jaxpr_2 = _move_and_pair_arg(lifted_jaxpr_2)
  sc_out = sc_c_out, sc_b_out = unknown(out_pv_c), unknown(out_pv_b)

  in_avals_1, in_avals_2 = unzip2(map(_split_avals, second_components,
                                      jaxpr.in_avals))
  out_aval_1, out_aval_2 = _split_avals(sc_out, jaxpr.out_aval)

  # in_avals_1 is already (d1, c1, a1), and out_aval_2 is already (c2, b2), but
  # we must munge:
  # 1. form out_aval_1 to include the residuals as (c1, (b1, res))
  # 2. form in_avals_2 to include the residuals as (d2, c2, (a2, res))

  out_pv, _ = out_pval
  _, (_, res) = out_pv
  assert isinstance(res, AbstractValue)

  c1, b1 = out_aval_1
  lifted_out_aval_1 = AbstractTuple((c1, AbstractTuple((b1, res))))

  d2, c2, a2 = in_avals_2
  lifted_in_avals_2 = (d2, c2, AbstractTuple((a2, res)))

  typed_jaxpr_1 = TypedJaxpr(jaxpr_1, consts_1, in_avals_1, lifted_out_aval_1)
  typed_jaxpr_2 = TypedJaxpr(doubly_lifted_jaxpr_2, (), lifted_in_avals_2,
                             out_aval_2)
  return typed_jaxpr_1, typed_jaxpr_2, sc_out

def _move_and_pair_arg(jaxpr):
  moved_jaxpr = jaxpr.copy()
  res, d, c, a = jaxpr.invars
  moved_jaxpr.invars = [d, c, (a, res)]
  core.skip_checks or core.check_jaxpr(moved_jaxpr)
  return moved_jaxpr

def _split_avals(second_component, aval):
  assert False, "update it"
  t = type(second_component)
  if t is tuple:
    assert type(aval) is AbstractTuple
    avals1, avals2 = unzip2(map(_split_avals, second_component, aval))
    return AbstractTuple(avals1), AbstractTuple(avals2)
  elif t is bool:
    if second_component:
      return AbstractTuple(()), aval
    else:
      return aval, AbstractTuple(())
  else:
    raise TypeError(t)


custom_partial_eval_rules = {}

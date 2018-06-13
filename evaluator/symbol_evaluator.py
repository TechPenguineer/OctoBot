import inspect
import logging

from config.cst import EvaluatorMatrixTypes, CONFIG_TRADER_MODE, CONFIG_TRADER, CONFIG_EVALUATORS_WILDCARD, \
    START_PENDING_EVAL_NOTE
from evaluator.evaluator_creator import EvaluatorCreator
from evaluator.evaluator_matrix import EvaluatorMatrix
from evaluator.TA import TAEvaluator
from evaluator.RealTime import RealTimeTAEvaluator
from evaluator.Social import SocialEvaluator
from evaluator.Strategies import StrategiesEvaluator
from trading.trader import modes
from trading.trader.modes import AbstractTradingMode


class SymbolEvaluator:
    def __init__(self, config, symbol, crypto_currency_evaluator):
        self.crypto_currency_evaluator = crypto_currency_evaluator
        self.symbol = symbol
        self.trader_simulator = None
        self.config = config
        self.traders = None
        self.trader_simulators = None
        self.logger = logging.getLogger("{0} {1}".format(self.symbol, self.__class__.__name__))

        self.evaluator_thread_managers = {}
        self.trading_mode_instances = {}
        self.matrices = {}
        self.strategies_eval_lists = {}
        self.finalize_enabled_list = {}

        self.trading_mode_class = self.get_trading_mode_class()

        self.all_TA_subclasses = None
        self.all_RT_subclasses = None
        self.all_social_subclasses = None
        self.all_strategies_subclasses = None

    def set_traders(self, trader):
        self.traders = trader

    def set_trader_simulators(self, simulator):
        self.trader_simulators = simulator

    def get_trading_mode_class(self):
        if CONFIG_TRADER in self.config and CONFIG_TRADER_MODE in self.config[CONFIG_TRADER]:
            if any(m[0] == self.config[CONFIG_TRADER][CONFIG_TRADER_MODE] and
                   hasattr(m[1], '__bases__') and
                   AbstractTradingMode in m[1].__bases__
                   for m in inspect.getmembers(modes)):
                return getattr(modes, self.config[CONFIG_TRADER][CONFIG_TRADER_MODE])

        raise Exception("Please specify a valid trading mode in your config file (trader -> mode)")

    def add_evaluator_thread_manager(self, exchange, symbol, time_frame, evaluator_thread):
        if exchange.get_name() in self.evaluator_thread_managers:
            self.evaluator_thread_managers[exchange.get_name()][time_frame] = evaluator_thread
        else:
            self.evaluator_thread_managers[exchange.get_name()] = {time_frame: evaluator_thread}
            self.trading_mode_instances[exchange.get_name()] = self.trading_mode_class(self.config, self, exchange,
                                                                                       symbol)

            self.matrices[exchange.get_name()] = EvaluatorMatrix(self.config)
            self.strategies_eval_lists[exchange.get_name()] = EvaluatorCreator.create_strategies_eval_list(self.config)
            self.finalize_enabled_list[exchange.get_name()] = False

    def update_strategies_eval(self, new_matrix, exchange, ignored_evaluator=None):
        for strategies_evaluator in self.get_strategies_eval_list(exchange):
            if strategies_evaluator.get_is_active():
                strategies_evaluator.set_matrix(new_matrix)
                if not strategies_evaluator.get_name() == ignored_evaluator and strategies_evaluator.get_is_evaluable():
                    strategies_evaluator.eval()

                new_matrix.set_eval(EvaluatorMatrixTypes.STRATEGIES, strategies_evaluator.get_name(),
                                    strategies_evaluator.get_eval_note())
            else:
                new_matrix.set_eval(EvaluatorMatrixTypes.STRATEGIES, strategies_evaluator.get_name(),
                                    START_PENDING_EVAL_NOTE)

    def _init_all_evaluator_classes_name_list_if_necessary(self):
        if self.all_TA_subclasses is None:
            self.all_TA_subclasses = [subclass.__name__ for subclass in TAEvaluator.get_all_subclasses()
                                      if subclass.is_enabled(self.config, False)]
        if self.all_RT_subclasses is None:
            self.all_RT_subclasses = [subclass.__name__ for subclass in RealTimeTAEvaluator.get_all_subclasses()
                                      if subclass.is_enabled(self.config, False)]
        if self.all_social_subclasses is None:
            self.all_social_subclasses = [subclass.__name__ for subclass in SocialEvaluator.get_all_subclasses()
                                          if subclass.is_enabled(self.config, False)]
        if self.all_strategies_subclasses is None:
            self.all_strategies_subclasses = [subclass.__name__ for subclass in StrategiesEvaluator.get_all_subclasses()
                                              if subclass.is_enabled(self.config, False)]

    def _get_evaluators_from_strategy(self, strategy, TA_list, RT_list, social_list):
        self._init_all_evaluator_classes_name_list_if_necessary()
        # add wildcard handling
        required_evaluators = strategy.get_required_evaluators()
        if required_evaluators == CONFIG_EVALUATORS_WILDCARD:
            TA_list.update(self.all_TA_subclasses)
            RT_list.update(self.all_RT_subclasses)
            social_list.update(self.all_social_subclasses)
        else:
            for evaluator in strategy.get_required_evaluators():
                if evaluator in self.all_TA_subclasses:
                    TA_list.add(evaluator)
                elif evaluator in self.all_RT_subclasses:
                    RT_list.add(evaluator)
                elif evaluator in self.all_social_subclasses:
                    social_list.add(evaluator)

    @staticmethod
    def _filter_and_activate_or_deactivate_evaluator(symbol, to_change_eval, to_keep_eval, activate, evaluator_instances):
        # add advanced classes management
        evaluator_instances_names = [evaluator.get_name() for evaluator in evaluator_instances]
        for evaluator in to_change_eval:
            if activate or evaluator not in to_keep_eval:
                evaluator_name_identifier = evaluator
                if evaluator not in evaluator_instances_names:
                    # try advanced classes
                    for instance in evaluator_instances:
                        bases = [base.__name__ for base in instance.get_parent_evaluator_classes()]
                        eval_name = instance.get_name()
                        if evaluator in bases and eval_name in evaluator_instances_names:
                            evaluator_name_identifier = eval_name
                if evaluator_name_identifier in evaluator_instances_names:
                    eval_instance = evaluator_instances[evaluator_instances_names.index(evaluator_name_identifier)]
                    if not activate and eval_instance.get_is_active():
                        eval_instance.reset()
                    eval_instance.set_is_active(activate)
                else:
                    logging.getLogger(SymbolEvaluator.__class__.__name__).error("error: {}{} not found in {}".
                                                                                format(symbol,
                                                                                       evaluator_name_identifier,
                                                                                       evaluator_instances_names))

    def activate_deactivate_strategies(self, strategies, exchange, activate=True):
        to_change_social = set()
        to_change_TA = set()
        to_change_RT = set()
        strategy_classes = [strat.__class__ for strat in self.get_strategies_eval_list(exchange)]
        strategy_classe_bases = set()
        [strategy_classe_bases.update(s.get_parent_evaluator_classes()) for s in strategy_classes]
        for strategy in strategies:
            if strategy in strategy_classe_bases:
                self._get_evaluators_from_strategy(strategy, to_change_TA, to_change_RT, to_change_social)

                strategy_to_find = strategy
                # get parent strategy if found in bases
                if strategy not in strategy_classes:
                    strategy_to_find = next(filter(lambda x: x in strategy_classes, strategy.get_all_subclasses()))

                strat_inst = self.get_strategies_eval_list(exchange)[strategy_classes.index(strategy_to_find)]
                strat_inst.set_is_active(activate)
                if not activate and strat_inst.get_is_active():
                    strat_inst.reset()
            else:
                raise RuntimeError("{0} strategy to be activated or deactivated is not in {1} symbol evaluator's "
                                   "strategies_eval_lists for {2} exchange.".format(strategy.get_name(), self.symbol,
                                                                                    exchange.get_name()))

        to_keep_TA = set()
        to_keep_RT = set()
        to_keep_social = set()
        for strategy in self.get_strategies_eval_list(exchange, True):
            self._get_evaluators_from_strategy(strategy, to_keep_TA, to_keep_RT, to_keep_social)

        thread_managers = self.evaluator_thread_managers[exchange.get_name()]
        if thread_managers:
            self._filter_and_activate_or_deactivate_evaluator(
                self.symbol, to_change_RT, to_keep_RT, activate,
                next(iter(thread_managers.values())).evaluator.get_real_time_eval_list())

        # only deactivate realtime evaluators and TA evaluators
        for evaluator_thread_manager in thread_managers.values():
            self._filter_and_activate_or_deactivate_evaluator(self.symbol, to_change_TA, to_keep_TA, activate,
                                                              evaluator_thread_manager.evaluator.get_ta_eval_list())
            # force refresh TA eval
            if activate:
                evaluator_thread_manager.get_evaluator().data_changed = True
                evaluator_thread_manager.get_evaluator().update_ta_eval()
            evaluator_thread_manager.refresh_matrix()

        # finally, refresh strategies
        self.update_strategies_eval(next(iter(thread_managers.values())).matrix, exchange, None)

        self.logger.info("{} activated: {}".format([s.get_name() for s in strategies], activate))


    def finalize(self, exchange):
        if not self.finalize_enabled_list[exchange.get_name()]:
            self._check_finalize(exchange)

        if self.finalize_enabled_list[exchange.get_name()]:
            self.trading_mode_instances[exchange.get_name()].get_decider().add_to_queue()

    def _check_finalize(self, exchange):
        self.finalize_enabled_list[exchange.get_name()] = True
        for evaluator_thread in self.evaluator_thread_managers[exchange.get_name()].values():
            if evaluator_thread.get_refreshed_times() == 0:
                self.finalize_enabled_list[exchange.get_name()] = False

    def get_trader(self, exchange):
        return self.traders[exchange.get_name()]

    def get_trader_simulator(self, exchange):
        return self.trader_simulators[exchange.get_name()]

    def get_final(self, exchange):
        return self.trading_mode_instances[exchange.get_name()].get_decider()

    def has_exchange(self, exchange):
        return exchange.get_name() in self.trading_mode_instances

    def get_matrix(self, exchange):
        return self.matrices[exchange.get_name()]

    def get_evaluator_thread_managers(self, exchange):
        return self.evaluator_thread_managers[exchange.get_name()]

    def get_config(self):
        return self.config

    def get_strategies_eval_list(self, exchange, active_only=False):
        if not active_only:
            return self.strategies_eval_lists[exchange.get_name()]
        else:
            return [strategy
                    for strategy in self.strategies_eval_lists[exchange.get_name()]
                    if strategy.get_is_active()]

    def get_symbol(self):
        return self.symbol

    def get_crypto_currency_evaluator(self):
        return self.crypto_currency_evaluator
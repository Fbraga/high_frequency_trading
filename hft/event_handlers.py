from .cache import (get_cache_key, write_to_cache_with_version, cache_timeout,
    get_trader_ids_by_market)
from . import utility
from .subject_state import SubjectStateFactory
from .trader import CDATraderFactory, FBATraderFactory, ELOTraderFactory 
from django.core.cache import cache
from .decorators import atomic
from random import shuffle
from otree.timeout.tasks import hft_background_task
from . import checkpoints

import logging
log = logging.getLogger(__name__)

trader_factory_map = {
    'BCS': {'CDA': CDATraderFactory, 'FBA': FBATraderFactory},
    'elo': {'CDA': ELOTraderFactory}
    }

SUBPROCESSES = {}

class HandlerFactory:

    @staticmethod
    def get_handler(handler_type):
        if handler_type == 'trader':
            return leeps_handle_trader_message
        elif handler_type == 'market':
            return leeps_handle_market_message
        elif handler_type == 'trade_session':
            return leeps_handle_session_events
        elif handler_type == 'noise_trader_arrival':
            return noise_trader_arrival
        elif handler_type == 'marketwide_events':
            return marketwide_events
        elif handler_type == 'role_based_events':
            return role_based_events
        else:
            raise Exception('unknown event: %s' % handler_type)

def leeps_handle_trader_message(event, exchange_format='CDA', session_format='elo', 
        **kwargs):
    player_id = event.attachments.get('player_id')
    if player_id is None:
        player_id = event.message.get('player_id')   
        if player_id is None:
            raise Exception('player id is missing in event %s.' % event)
    if player_id == 0:
        event.attachments['note'] = 'investor'
        hft_background_task(checkpoints.hft_investor_checkpoint, event)
        return event
    key = get_cache_key(player_id ,'trader')
    trader_data = cache.get(key)
    if trader_data is None:
        raise ValueError('trader key: %s returned none.' % key)
    if event.event_type == 'role_change':
        trader_data['role'] = event.message.get('state')
    role_name, subject_state = trader_data['role'], trader_data['subject_state']
    TraderFactory = trader_factory_map[session_format][exchange_format]
    trader = TraderFactory.get_trader(role_name, subject_state)
    fields = event.message.copy()
    fields.update(event.attachments)
    trader.receive(event.event_type, **fields)
    message_queue = trader.outgoing_messages.copy()
    trader.outgoing_messages.clear()
    event.outgoing_messages.extend(message_queue)
    state_cls = SubjectStateFactory.get_state(session_format)
    trader_state = state_cls.from_trader(trader)
    trader_data['subject_state'] = trader_state
    version = trader_data['version'] + 1
    try:
        write_to_cache_with_version(key, trader_data, version)
    except ValueError:
        leeps_handle_trader_message(event, **kwargs)
    else:
        hft_background_task(checkpoints.hft_trader_checkpoint, player_id, 
            trader_state, event)  
        return event

def leeps_handle_market_message(event, **kwargs):
    market_id = event.message.get('market_id')
    if market_id is None:
        market_id = event.attachments.get('market_id')
    market_key = get_cache_key(market_id, 'market')
    market_data = cache.get(market_key)
    if market_data is None:
        raise Exception('market key: %s returned none, event: %s' % (market_key,
            event))
    market, version = market_data['market'], market_data['version']
    fields = utility.kwargs_from_event(event)
    attachments = market.receive(event.event_type, **fields)
    if attachments:
        event.attachments.update(attachments)
    message_queue = market.outgoing_messages.copy()
    if event.event_type in market.attachments_for_observers:
        event.attachments.update(market.attachments_for_observers[event.event_type])
    market.outgoing_messages.clear()
    event.outgoing_messages.extend(message_queue)
    market_data['market'] = market
    version = market_data['version'] + 1
    try:
        write_to_cache_with_version(market_key, market_data, version)
    except ValueError:
        leeps_handle_market_message(event, **kwargs)
    else:
        return event

@atomic
def leeps_handle_session_events(event, **kwargs):
    message_type, market_id = event.event_type, event.message['market_id']
    subsession_id = event.message['subsession_id']
    session_key = get_cache_key(subsession_id, 'trade_session')
    trade_session = cache.get(session_key)
    if trade_session.id not in SUBPROCESSES:
        SUBPROCESSES[trade_session.id] = {}
    trade_session.clients = SUBPROCESSES[trade_session.id]      
    trade_session.receive(message_type, market_id)
    SUBPROCESSES[trade_session.id] = trade_session.clients
    trade_session.clients = {}
    message_queue = trade_session.outgoing_messages.copy()
    trade_session.outgoing_messages.clear()
    event.outgoing_messages.extend(message_queue)    
    cache.set(session_key, trade_session, timeout=cache_timeout)
    return event

integer_fields = ('price', 'time_in_force')
def noise_trader_arrival(event, **kwargs):
    event.attachments['market_id'] = str(event.message['market_id'])
    event.message['price'] = int(event.message['price'] )
    event.message['time_in_force'] = int(event.message['time_in_force'])
    event = leeps_handle_market_message(event, **kwargs)
    return event

def marketwide_events(event, **kwargs):
    market_id = event.message['market_id']
    event.attachments['market_id'] = market_id
    traders_in_market = get_trader_ids_by_market(market_id)
    for trader_id in traders_in_market:
        event.attachments['player_id'] = trader_id
        event = leeps_handle_trader_message(event, **kwargs)
    shuffle(event.outgoing_messages)
    return event

def role_based_events(event, **kwargs):
    market_id = event.message['market_id']
    event.attachments['market_id'] = market_id
    makers = event.message['maker_ids']
    for trader_id in makers:
        event.attachments['player_id'] = trader_id
        event = leeps_handle_trader_message(event, **kwargs)
    shuffle(event.outgoing_messages)
    return event


# def fundamental_price_change(event, **kwargs):
#     market_id = int(event.message['market_id'])
#     all_players_data = get_players_by_market(market_id)
#     for player_data in all_players_data:
#         player = player_data['model']
#         event.message['player_id'] = player.id
#         event = leeps_handle_trader_message(event, **kwargs)
#     shuffle(event.outgoing_messages)
#     event = leeps_handle_market_message(event, **kwargs)
#     return event

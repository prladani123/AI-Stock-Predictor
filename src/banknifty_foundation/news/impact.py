"""Conservative headline stance. Ambiguous or opposing cues abstain."""
import re

class EventImpactClassifier:
    def classify(self, headline, category):
        text=headline.lower()
        rules={
            'CRUDE_OIL':('RISING','FALLING',r'\b(oil|crude|brent)\b.{0,25}\b(rises?|rising|surges?|jumps?|higher|rallies)\b',r'\b(oil|crude|brent)\b.{0,25}\b(falls?|falling|drops?|retreats?|eases?|easing|lower|slips?)\b','BEARISH','BULLISH'),
            'RBI':('HAWKISH','DOVISH',r'\b(hikes?|raises?|hawkish)\b',r'\b(cuts?|lowers?|dovish)\b','BEARISH','BULLISH'),
            'FED':('HAWKISH','DOVISH',r'\b(hikes?|raises?|hawkish)\b',r'\b(cuts?|lowers?|dovish)\b','BEARISH','BULLISH'),
            'GEOPOLITICS':('ESCALATION','DEESCALATION',r'\b(attacks?|escalat\w*|invades?|war erupts)\b',r'\b(ceasefire|peace agreement|de-escalat\w*)\b','BEARISH','BULLISH'),
            'INFLATION':('HOTTER_THAN_EXPECTED','COOLER_THAN_EXPECTED',r'\b(hotter|above forecasts?|higher than expected)\b',r'\b(cooler|below forecasts?|lower than expected)\b','BEARISH','BULLISH'),
            'BANK_EARNINGS':('POSITIVE','NEGATIVE',r'\b(beats? estimates?|profit rises|profit jumps)\b',r'\b(misses? estimates?|profit falls|loss widens)\b','BULLISH','BEARISH')}
        if category not in rules:return {'stance':'UNKNOWN','directional_context':'UNKNOWN'}
        a,b,positive,negative,da,db=rules[category]
        pa,pb=bool(re.search(positive,text)),bool(re.search(negative,text))
        if pa==pb:
            stance='MIXED' if pa else 'UNCHANGED' if category in {'RBI','FED'} and re.search(r'unchanged|holds? rates?|rates? hold',text) else 'UNKNOWN'
            return {'stance':stance,'directional_context':'UNKNOWN'}
        # Hypothetical forecasts do not establish an actual change.
        if re.search(r'\b(may|could|expected to|bets on)\b',text):return {'stance':'UNKNOWN','directional_context':'UNKNOWN'}
        return {'stance':a if pa else b,'directional_context':da if pa else db}

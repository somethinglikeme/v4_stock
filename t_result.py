from dotenv import load_dotenv; load_dotenv()
import sys
from meritz_wrapper import get_order_results
print(get_order_results([{"order_no": sys.argv[1], "submitted_at": sys.argv[2]}]))
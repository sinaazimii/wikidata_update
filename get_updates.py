import requests
import re
import sys
from datetime import datetime
from bs4 import BeautifulSoup
from rdflib import Graph, Namespace
import new_entity_rdf
import argparse
from dateutil.relativedelta import relativedelta

# default values
CHANGES_TYPE = "edit|new"
CHANGE_COUNT = 5
LATEST = "false"
START_DATE = None
END_DATE = None
FILE_NAME = None

# Define prefixes for the SPARQL query
WD = "PREFIX wd: <http://www.wikidata.org/entity/>"
WDT = "PREFIX wdt: <http://www.wikidata.org/prop/direct/>"
SCHEMA = "PREFIX schema: <http://schema.org/>"
SKOS = "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>"
WIKIBASE = "PREFIX wikibase: <http://wikiba.se/ontology#>"

# Define namespaces
PREFIXES = WD + "\n" + WDT + "\n" + SCHEMA + "\n" + SKOS + "\n" + WIKIBASE + "\n"

EDIT_DELETE_RDFS = []
EDIT_INSERT_RDFS = []
NEW_INSERT_RDFS = []


def get_wikidata_updates(start_time, end_time):
    # Construct the API request URL
    api_url = "https://www.wikidata.org/w/api.php"
    params = {
        "action": "query",
        "list": "recentchanges",
        "rcstart": end_time,
        "rcend": start_time,
        "rclimit": CHANGE_COUNT,
        "rcprop": "title|ids|sizes|flags|user|timestamp",
        "format": "json",
        "rctype": CHANGES_TYPE,  # Limit the type of changes to edits and new entities
    }
    # Make the request
    response = requests.get(api_url, params=params)
    data = response.json()
    # Check for errors in the response
    if "error" in data:
        print("Error:", data["error"]["info"])
        return
    changes = data.get("query", {}).get("recentchanges", [])
    return changes


def compare_changes(api_url, change):
    global NEW_INSERT_RDFS
    new_rev = change["revid"]
    old_rev = change["old_revid"]
    diff = ""
    if change["type"] == "new":
        # Fetch the JSON data for the new entity
        new_insert_statement = new_entity_rdf.main(change["title"])
        print(new_insert_statement)
        NEW_INSERT_RDFS.append((change['title'],new_insert_statement, change["timestamp"]))
        return
    elif change["type"] != "edit":
        print("Unsupported change type:", change["type"])
        return
    elif change["type"] == "edit":
        params = {
            "action": "compare",
            "fromrev": old_rev,
            "torev": new_rev,
            "format": "json",
        }

        response = requests.get(api_url, params=params)
        comparison_data = response.json()
        if "compare" in comparison_data:
            # Fetch The HTML diff of the changes using compare API
            diff = comparison_data["compare"]["*"]
            convert_to_rdf(diff, change["title"], change["timestamp"])
        else:
            print("Comparison data unavailable.")
    return diff


def convert_to_rdf(diff_html, entity_id, timestamp):
    # need a subject, predicate and object for each change
    subject = entity_id
    soup = BeautifulSoup(diff_html, "html.parser")
    # Construct DELETE and INSERT statements
    delete_statements = []
    insert_statements = []
    global PREFIXES, EDIT_DELETE_RDFS, EDIT_INSERT_RDFS
    rows = soup.find_all("tr")
    current_predicate = None
    for row in rows:
        # Process property names
        if row.find("td", class_="diff-lineno"):
            td_tag_text = row.get_text(strip=True)
            value = row.find("a")
            if value:
                pattern = re.compile(r'/wiki/Property:(P\d+)')
                if (value and pattern.search(value.prettify())):
                    # Extract the property ID from the match
                    property_id = pattern.search(value.prettify()).group(1)
                    current_predicate = f"wdt:{property_id}"
                    sub_props = td_tag_text.split("/")[2:]
                    for sub_prop in sub_props:
                        current_predicate += f"/{sub_prop.strip()}"
            else:
                current_predicate = (
                    f"schema:{row.find('td', class_='diff-lineno').text.strip().replace(' ', '')}"
                )

        # TODO: schema url? is description really a schema property?

        # Process deleted values
        if row.find("td", class_="diff-deletedline"):
            value = row.find("del", class_="diffchange")
            if value and current_predicate:
                deleted_value = value.text.strip()
                delete_statements.append(
                    f'  wd:{subject} {current_predicate} "{deleted_value}" .'
                )

        # Process added values
        elif row.find("td", class_="diff-addedline"):
            value = row.find("ins", class_="diffchange")
            # find all nested a_tags/b_tags in the value
            if value:
                nested_tags = value.find_all("a")
                nested_tags += value.find_all("b")
                if(len(nested_tags) > 0 and len(nested_tags) % 2 == 0):
                    insert_statements.append(handle_nested(nested_tags, current_predicate, subject))
                else:
                    if current_predicate:
                        added_value = value.text.strip()
                        insert_statements.append(
                            f'  wd:{subject} {current_predicate} "{added_value}" .'
                        )


    delete_rdf = "DELETE DATA {\n" + "\n".join(delete_statements) + "\n};"
    insert_rdf = "INSERT DATA {\n" + "\n".join(insert_statements) + "\n};"

    if delete_statements != []:
        EDIT_DELETE_RDFS.append((subject, delete_rdf, timestamp))
        print(delete_rdf)
        print("\n")
    if insert_statements != []:
        EDIT_INSERT_RDFS.append((subject, insert_rdf,timestamp))
        print(insert_rdf)
        print("\n")

def handle_nested(nested_tags, current_predicate, subject):
    insert_statement = ""
    for i in range(0, len(nested_tags), 2):
        # check if a tag has a property id
        # its a predicate
        pattern = re.compile(r'/wiki/Property:(P\d+)')
        if pattern.search(nested_tags[i].get('href')):
            property_id = ":" +pattern.search(nested_tags[i].get('href')).group(1)
            insert_statement = f'  wd:{subject} {f"{current_predicate + property_id}"} "{nested_tags[i+1].text}" .'

    return insert_statement

def verify_args(args):
    global CHANGES_TYPE, CHANGE_COUNT, LATEST, START_DATE, END_DATE, FILE_NAME
    if args.latest and (args.start or args.end):
        print("Cannot set latest and start or end date at the same time.")
        return False
    if args.start and not args.end:
        print("Cannot set start date without end date.")
        return False
    if args.end and not args.start:
        print("Cannot set end date without start date.")
        return False
    if args.type:
        if args.type not in ["edit|new", "edit", "new"]:
            print(
                "Invalid type argument. Please provide 'edit' or 'new, not setting means both of them'."
            )
            return False
        else:
            CHANGES_TYPE = args.type
    if args.latest:
        if args.latest not in ["true", "false"]:
            print("Invalid type argument. Please provide 'true' or 'false'.")
            return False
        else:
            LATEST = args.latest
    if args.file:
        if not args.file.endswith(".ttl") and not args.file.endswith(".txt"):
            print(
                "Invalid file name. Please provide a file with .ttl or .txt extension."
            )
            return False
        FILE_NAME = args.file
    if args.number:
        try:                
            if not int(args.number) or int(args.number) not in range(1, 501):
                print("Invalid number argument. Please provide a valid number between 1 and 501.")
                return False
            else:
                CHANGE_COUNT = args.number
        except ValueError:
            print("Invalid number argument. Please provide a valid number between 1 and 500.")
            return False
    if args.start:
        if verify_date(args.start):
            START_DATE = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
        else:
            print("Invalid start date argument. Please provide a valid date.")
            return False
    if args.end:
        if verify_date(args.end):
            END_DATE = datetime.strptime(args.end, "%Y-%m-%d %H:%M:%S")
        else:
            print("Invalid end date argument. Please provide a valid date.")
            return False
    if not args.start or not args.end:
        LATEST = "true"

    if START_DATE and END_DATE:
        if (END_DATE - START_DATE).days < 0:
            print("Start date cannot be later than end date.")
            return False

    return True


def verify_date(date):
    if (
        type(date) is not str
        or len(date) != 19
        or date[10] != " "
        or date[13] != ":"
        or date[16] != ":"
        or date[4] != "-"
        or date[7] != "-"
        or int(date[0:4]) not in range(1000, 9999)
        or int(date[5:7]) not in range(1, 12)
        or int(date[8:10]) not in range(1, 31)
        or int(date[11:13]) not in range(0, 24)
        or int(date[14:16]) not in range(0, 60)
        or int(date[17:19]) not in range(0, 60)
    ):
        return False
    # check if date is not earlier than 1 month ago from now
    formatted_date = datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    one_month_ago = now - relativedelta(months=1)
    if formatted_date < one_month_ago:
        print("The date cannot be earlier than 1 month ago.")
        return False
    return True


def write_to_file(data):
    print("Writing changes to file...")
    with open(FILE_NAME, "w") as file:
        file.write(PREFIXES)
        file.write("\n")
        for subject, change, time in data:
            file.write(change)
            file.write("\n")
    print("Changes written to file.")

# def compress_changes(changes_list):
#     # changes list are in form of (subject, rdf, timestamp)
#     compressed_changes = []
#     for i in range  (0,len(changes_list)):
#         for j in range (i+1,len(changes_list)):
#             if changes_list[i][0] == changes_list[j][0]:
#                 compressed_changes.append(merge_rdf(changes_list[i][1], changes_list[j][1]))
#                 changes_list.pop(j)
#     return compressed_changes

def merge_rdf(old_rdf, new_rdf):
    # Extract the content from the old string's curly braces
    old_match = re.search(r'\{(.*?)\}', old_rdf, re.DOTALL)
    if old_match:
        old_content = old_match.group(1).strip()
    else:
        old_content = ''

    # Insert the old content into the new string's curly braces
    new_match = re.search(r'\{(.*?)\}', new_rdf, re.DOTALL)
    if new_match:
        new_content = new_match.group(1).strip()
        combined_content = f"{new_content}\n  {old_content}"  # Add old content with a new line and indent
        # Replace the content inside the new string's curly braces
        updated_string = re.sub(r'\{(.*?)\}', f'{{\n  {combined_content}\n}}', new_rdf, flags=re.DOTALL)
        return updated_string

def main():
    # define some command line arguments
    parser = argparse.ArgumentParser(
        description="This script retrieves recent changes of the wikidata, allowing you to store the output in a file"
        "not setting a time period will get the latest changes"
    )
    parser.add_argument("-f", "--file", help="store the output in a file")
    parser.add_argument("-l", "--latest", help="get latest changes")
    parser.add_argument(
        "-t",
        "--type",
        help="filter the type of changes. possible values are edit|new, edit, new",
    )
    parser.add_argument(
        "-n",
        "--number",
        help="number of changes to get, not setting will get 5 changes, Maximum number of changes is 501",
    )
    parser.add_argument(
        "-st",
        "--start",
        help="start date and time, in form of 'YYYY-MM-DD HH:MM:SS, not setting start and end date will get latest changes",
    )
    parser.add_argument(
        "-et","--end", help="end date and time, in form of 'YYYY-MM-DD HH:MM:SS'"
    )
    args = parser.parse_args()

    # verify the arguments type and values
    if verify_args(args):
        print("Getting updates from Wikidata...")
        print("Type: ", CHANGES_TYPE)
        print("Latest: ", LATEST)
        print("Number: ", CHANGE_COUNT)
        print("Start Date: ", START_DATE)
        print("End Date: ", END_DATE)
        print("File Name: ", FILE_NAME)
        print("\n")
        print(PREFIXES)
        changes = get_wikidata_updates(START_DATE, END_DATE)
        # Calling compare changes with the first change in the list for demonstration
        i=0
        for change in changes:
            print(i)
            compare_changes("https://www.wikidata.org/w/api.php", change)
            i+=1
        # write the changes to a file
        if FILE_NAME:
            # merge all the changes into one list sorted by timestamp
            all_changes = sorted(
                EDIT_DELETE_RDFS + EDIT_INSERT_RDFS + NEW_INSERT_RDFS, key=lambda x: x[1]
            )
            write_to_file(all_changes)
            print(len(all_changes))
            print(len(compress_changes(all_changes)))

            # TODO: some rdfs still are ill formed, sub props are not handled properly in some cases. Investigate!
            # TODO: Comparison not working as intended! 1477 is the new length of the list after compression (500 before) 

            # Possible refinement: stream the changes to the file while processing them to save time and memory
            # TODO: Add command line argument for filtering language of the new entities, without filtering the language
            # the script will get all the languages of the new entity and resulting rdf might be too large


main()

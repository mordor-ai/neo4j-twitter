import argparse
import sys
import os
import tweepy
import csv
import json
import calendar

from collections import  deque
from util import Users
from py2neo import Graph
from dateutil import parser

def seed(api, username):
    if os.path.exists("data/users.csv"):
        print "Twitter graph has already been seeded. Delete 'data/users.csv' if you want to seed it again"
        sys.exit(1)

    USERS_TO_PROCESS = 50
    users_to_process = deque()
    users_processed = set([username])

    for tweet in tweepy.Cursor(api.user_timeline, id=username).items(50):
        for user in tweet.entities["user_mentions"]:
            if not len(users_to_process) > USERS_TO_PROCESS:
                users_to_process.append(user["screen_name"])
            else:
                break
    users_processed = set([username])
    while True:
        if len(users_processed) >= USERS_TO_PROCESS:
            break
        else:
            if len(users_to_process) > 0:
                next_user = users_to_process.popleft()
                print next_user
                if not next_user in users_processed:
                    users_processed.add(next_user)
                    for tweet in tweepy.Cursor(api.user_timeline, id=next_user).items(10):
                        for user_mentioned in tweet.entities["user_mentions"]:
                            if not len(users_processed) > 50:
                                users_to_process.append(user_mentioned["screen_name"])
                            else:
                                break
            else:
                break
    with open("data/users.csv", "w") as usersfile:
        writer = csv.writer(usersfile, delimiter=",")
        for user in users_processed:
            writer.writerow([user, "", ""])

def read_user(username):
    print username
    profile_file_path = "data/profiles/{0}.json".format(username)
    if os.path.exists(profile_file_path):
        with open(profile_file_path, "r") as file:
            profile = json.loads(file.read())
            print profile["name"]
            print profile["description"]
            print "Friends: {0}".format(len(profile["friends"]))
            print "Followers: {0}".format(len(profile["followers"]))

    file_path = "data/tweets/{0}.json".format(username)

    if not os.path.exists(file_path):
        tweets = []
    else:
        with open(file_path, "r") as file:
            tweets = json.loads(file.read())

    print "# of tweets: {0}".format(len(tweets))
    if len(tweets) > 0:
        print "latest tweets:"
        for tweet in tweets:
            print tweet["id"], tweet["text"]

def download_all_user_tweets(api, users):
    unprocessed_users =  [user[0] for user in users.all().iteritems()]
    for user in unprocessed_users:
        download_user_tweets(api, users, user)

def download_new_user_tweets(api, users):
    unprocessed_users =  [user[0] for user in users.all().iteritems() if not user[1]["lastTweetRetrieved"]]
    for user in unprocessed_users:
        download_user_tweets(api, users, user)

def download_all_user_profiles(api, users):
    unprocessed_users =  [user[0] for user in users.all().iteritems()
                          if not os.path.exists("data/profiles/{0}.json".format(user[0]))]

    for user in unprocessed_users:
        download_profile(api, user)

def download_user_tweets(api, users, username):
    print username
    value = users.find(username)

    file_path = "data/tweets/{0}.json".format(username)
    if os.path.exists(file_path):
        with open(file_path, "r") as file:
            tweets =  json.loads(file.read())
    else:
        tweets = []

    first_tweet_done = False
    since_id = value["lastTweetRetrieved"]
    for tweet in tweepy.Cursor(api.user_timeline, id=username, since_id = since_id).items(50):
        if not first_tweet_done:
            value["lastTweetRetrieved"] = tweet.id
            first_tweet_done = True
        tweets.append(tweet._json)

    users.save(username, value)

    with open("data/tweets/{0}.json".format(username), "w") as file:
        file.write(json.dumps(tweets))

def download_profile(api, username):
    print username

    profile = api.get_user(username)._json
    followers = list(tweepy.Cursor(api.followers_ids, username).items())
    friends = list(tweepy.Cursor(api.friends_ids, username).items())

    profile["followers"] =  followers
    profile["friends"] =  friends

    with open("data/profiles/{0}.json".format(username), "w") as file:
        file.write(json.dumps(profile))

def import_profiles_into_neo4j():
    graph = Graph()

    tx = graph.cypher.begin()
    files = [file for file in os.listdir("data/profiles") if file.endswith("json")]
    for file in files:
        with open("data/profiles/{0}".format(file), "r") as file:
            profile = json.loads(file.read())
            print profile["screen_name"]

            params = {
                "twitterId" : profile["id"],
                "screenName": profile["screen_name"],
                "name": profile["name"],
                "description": profile["description"],
                "followers" : profile["followers"],
                "friends" : profile["friends"]
            }
            statement = """
                        MERGE (p:Person {twitterId: {twitterId}})
                        REMOVE p:Shadow
                        SET p.screenName = {screenName},
                            p.description = {description},
                            p.name = {name}
                        WITH p

                        FOREACH(followerId IN {followers} |
                          MERGE (follower:Person {twitterId: followerId})
                          ON CREATE SET follower:Shadow
                          MERGE (follower)-[:FOLLOWS]->(p)
                        )

                        FOREACH(friendId IN {friends} |
                          MERGE (friend:Person {twitterId: friendId})
                          ON CREATE SET friend:Shadow
                          MERGE (p)-[:FOLLOWS]->(friend)
                        )
                        """
            tx.append(statement, params)

            tx.process()
    tx.commit()

def import_tweets_into_neo4j():
    graph = Graph()

    tx = graph.cypher.begin()
    count = 0

    files = [file for file in os.listdir("data/tweets") if file.endswith("json")]
    for file in files:
        with open("data/tweets/{0}".format(file), "r") as file:
            tweets = json.loads(file.read())

            for tweet in tweets:
                created_at = calendar.timegm(parser.parse(tweet["created_at"]).timetuple())

                params = {
                    "tweetId": tweet["id"],
                    "createdAt": created_at,
                    "text": tweet["text"],
                    "userId": tweet["user"]["id"],
                    "inReplyToTweetId": tweet["in_reply_to_status_id"],
                    "userMentions": [user for user in tweet["entities"]["user_mentions"]],
                    "urls": [url for url in tweet["entities"]["urls"]]
                }

                statement = """
                            MERGE (tweet:Tweet {id: {tweetId}})
                            SET tweet.text = {text}, tweet.timestamp = {createdAt}
                            REMOVE tweet:Shadow
                            WITH tweet
                            MATCH (person:Person {twitterId: {userId}})
                            MERGE (person)-[:TWEETED]->(tweet)
                            WITH tweet

                            FOREACH(user in {userMentions} |
                                MERGE (mentionedUser:Person {twitterId: user.id})
                                SET mentionedUser.screenName = user.screen_name
                                MERGE (tweet)-[:MENTIONED_USER]->(mentionedUser)
                            )

                            FOREACH(url in {urls} |
                                MERGE (u:URL {value: url.expanded_url})
                                MERGE (tweet)-[:MENTIONED_URL]->(u)
                            )

                            FOREACH(ignoreMe in CASE WHEN NOT {inReplyToTweetId} is null THEN [1] ELSE [] END |
                                MERGE (inReplyToTweet:Tweet {id: {inReplyToTweetId}})
                                ON CREATE SET inReplyToTweet:Shadow
                                MERGE (tweet)-[:IN_REPLY_TO_TWEET]->(inReplyToTweet)
                            )
                            """
                tx.append(statement, params)
                tx.process()
    tx.commit()

def add_new_users(users, count):
    graph = Graph()
    params = {"limit": count}
    results = graph.cypher.execute("""
                                  match (p:Shadow:Person)<-[:MENTIONED_USER]-(user)
                                  RETURN p.screenName AS user, COUNT(*) AS times
                                  ORDER BY times DESC
                                  LIMIT {limit}
                                  """, params)
    print results
    for row in results:
        users.add(row["user"])

def main(argv=None):
    parser = argparse.ArgumentParser(description='Query the Twitter API')

    # specific user
    parser.add_argument('--seed')
    parser.add_argument('--download-tweets')
    parser.add_argument('--download-profile')
    parser.add_argument('--read-user')

    parser.add_argument('--add-new-users', type=int)

    # all users
    parser.add_argument('--download-all-user-tweets', action='store_true')
    parser.add_argument('--download-new-user-tweets', action='store_true')
    parser.add_argument('--download-all-user-profiles', action='store_true')

    # twitter auth
    parser.add_argument('--check-auth', action='store_true')

    # import
    parser.add_argument('--import-profiles-into-neo4j', action='store_true')
    parser.add_argument('--import-tweets-into-neo4j', action='store_true')

    if argv is None:
        argv = sys.argv

    args = parser.parse_args()

    if args.read_user:
        read_user(args.read_user)
        return

    # Options that require keys go below here
    consumer_key =  os.environ.get('CONSUMER_KEY')
    consumer_secret =  os.environ.get('CONSUMER_SECRET')
    access_token =  os.environ.get('ACCESS_TOKEN')
    access_token_secret =  os.environ.get('ACCESS_TOKEN_SECRET')

    if any([key is None for key in [consumer_key, consumer_secret, access_token, access_token_secret]]):
        print "One of your twitter keys isn't set - don't forget to 'source credentials.local'"
        sys.exit(1)

    if args.check_auth:
        print "consumer_key: {0}".format(consumer_key)
        print "consumer_secret: {0}".format(consumer_secret)
        print "access_token: {0}".format(access_token)
        print "access_token_secret: {0}".format(access_token_secret)

        try:
            auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
            auth.set_access_token(access_token, access_token_secret)
            api = tweepy.API(auth, wait_on_rate_limit = True, wait_on_rate_limit_notify = True)
            api.verify_credentials()
            print "Auth all working - we're good to go!"
        except tweepy.TweepError as e:
            print "Auth problem - " + str(e)

        return

    auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
    auth.set_access_token(access_token, access_token_secret)
    api = tweepy.API(auth, wait_on_rate_limit = True, wait_on_rate_limit_notify = True)
    api.verify_credentials()

    if args.seed:
        seed(api, args.seed)
        return

    if args.download_tweets:
        users = Users()
        download_user_tweets(api, users,  args.download_tweets)
        return

    if args.download_all_user_tweets:
        users = Users()
        download_all_user_tweets(api, users)
        return

    if args.download_new_user_tweets:
        users = Users()
        download_new_user_tweets(api, users)
        return

    if args.download_profile:
        users = Users()
        download_profile(api, args.download_profile)
        return

    if args.download_all_user_profiles:
        users = Users()
        download_all_user_profiles(api, users)
        return

    if args.add_new_users:
        users = Users()
        add_new_users(users, args.add_new_users)
        return

    if args.import_profiles_into_neo4j:
        import_profiles_into_neo4j()
        return

    if args.import_tweets_into_neo4j:
        import_tweets_into_neo4j()
        return

if __name__ == "__main__":
    sys.exit(main())

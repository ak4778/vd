while true;do
curl --location 'http://10.65.58.26:8000/api/nodes/get?page=4&pageSize=50&t=1785833110703&isOnline=0,1&cameraType=1,2,3&operation=0%2C1%2C2%2C3%2C4&access_token=m4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'

curl --location 'http://10.65.58.26:8000/api/nodes/queryCategory' \
--header 'apitoken: m4h38NPRPB6CCZg6ZtQncinBcj5X4351Jd6PAOqd1v4wze4MNopW1CyC10Y5Ur6x'
done
